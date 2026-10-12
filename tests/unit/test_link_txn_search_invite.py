"""
Смена ключей линка на шлюзе: приглашение (проверка новых ключей, повтор,
замена более новым, отказ), ответ человека и отзыв сервером.
"""
from __future__ import annotations

import pytest

from awgbot.domain import gwcfg
from awgbot.domain.gateway.cfg import rekey as gwrekey
from tests.unit import _link_txn_common as _c
from tests.unit._link_txn_common import QUIET, _msg, _next_conf, _switched
from tests.unit.test_gwlink_rekey import gw_conf, keypair

pytestmark = pytest.mark.unit

conf_dir, svc, clock, host = _c.conf_dir, _c.svc, _c.clock, _c.host


# ── приглашение ──────────────────────────────────────────────────────────────

def test_new_keys_are_checked_kept_aside_and_asked_about(svc, host):
    """link_next прошёл проверку: новый конфиг — в rekey.next (0600, со
    штампом), живой не тронут, состояние asked с отпечатком пары, серверу —
    link_ack asked, человеку — приглашение первым в очереди вопросов."""
    conf, pub, vpub = _next_conf(host)
    res = svc.rekey_next_in(_msg(conf))
    assert res == {"reply": {"id": "a1b2c3d4e5f6", "state": "asked", "error": ""}, "ask": True,
                   "replaced": None}, res
    assert host.installed() == host.standing, "живой конфиг линка тронут до согласия человека"
    nxt = host.etc / "rekey.next"
    assert nxt.stat().st_mode & 0o077 == 0, "rekey.next с приватным ключом читается не только root"
    assert gwcfg.link_print(nxt.read_text(encoding="utf-8")) == gwcfg.link_print(conf)
    assert nxt.read_text(encoding="utf-8").startswith("# awg-bot: контракт линка 1\n")
    st = host.txn()
    assert st["state"] == "asked" and st["fp"] == gwcfg.key_fp(pub, vpub), st
    assert svc.rekey_question() == {"section": "link", "fp": gwcfg.key_fp(pub, vpub), "carries": False}
    assert svc.cfg_questions()[0] == "link" and svc.cfg_question("link")["fp"] == st["fp"]
    assert host.link_runs == [], "скрипт линка звали до согласия"


def test_the_same_transition_again_is_not_asked_twice(svc, host):
    """Сессия упала между asked и ready, сервер повторил link_next с тем же
    id: шлюз узнаёт его — второго приглашения нет, серверу — своё состояние
    (asked, а после «Применить» — ready: сервер сразу ведёт переход)."""
    conf, _, _ = _next_conf(host)
    svc.rekey_next_in(_msg(conf))
    again = svc.rekey_next_in(_msg(conf))
    assert again == {"reply": {"id": "a1b2c3d4e5f6", "state": "asked", "error": ""}, "ask": False,
                     "replaced": None}
    svc.rekey_answer(True)
    after = svc.rekey_next_in(_msg(conf))
    assert after["reply"]["state"] == "ready" and after["ask"] is False
    assert host.txn()["state"] == "ready", "повтор link_next откатил согласие человека"


def test_a_newer_invitation_replaces_the_shown_one(svc, host):
    """Новый id, пока висит прежнее приглашение: вопрос — о новом, а
    сообщение прежнего отдаётся, чтобы его поправить («заменён более новым»)."""
    conf, _, _ = _next_conf(host)
    svc.rekey_next_in(_msg(conf))
    svc.rekey_mark_msg(555, 42)
    newer, pub, vpub = _next_conf(host)
    res = svc.rekey_next_in(_msg(newer, "bbbb22223333"))
    assert res["ask"] is True and res["replaced"] == [555, 42], res
    assert host.txn()["id"] == "bbbb22223333" and host.txn()["fp"] == gwcfg.key_fp(pub, vpub)
    assert "msg" not in host.txn(), "сообщение прежнего приглашения приписано новому"


def test_a_newer_invitation_after_consent_replaces_it_too(svc, host):
    """Согласие дано, а сервер прислал новую стадию (прежнюю снял): прежнее
    сообщение — «заменён», вопрос — о новом."""
    conf, _, _ = _next_conf(host)
    svc.rekey_next_in(_msg(conf))
    svc.rekey_mark_msg(555, 42)
    svc.rekey_answer(True)
    newer, _, _ = _next_conf(host)
    res = svc.rekey_next_in(_msg(newer, "bbbb22223333"))
    assert res["ask"] is True and res["replaced"] == [555, 42], res
    assert host.txn()["state"] == "asked"


def test_the_invitation_message_is_remembered_only_while_asking(svc, host):
    conf, _, _ = _next_conf(host)
    svc.rekey_mark_msg(555, 1)
    assert host.txn() == {}
    svc.rekey_next_in(_msg(conf))
    svc.rekey_mark_msg(555, 42)
    assert host.txn()["msg"] == [555, 42]


@pytest.mark.parametrize("old, new, why", [
    ("Address = 10.99.99.2/30", "Address = 10.99.98.2/30", "Address"),
    ("Jc = 4\n", "Jc = 4\nPostUp = /bin/sh -c id\n", "PostUp"),
])
def test_new_keys_with_another_address_or_a_foreign_key_are_declined(svc, host, old, new, why):
    """Адрес /30 другой (линк не сойдётся) или ключ вне белого списка
    (PostUp исполнился бы от root) — отказ без вопроса, серверу — declined с
    причиной, файлов нет."""
    conf, _, _ = _next_conf(host)
    assert old in conf
    res = svc.rekey_next_in(_msg(conf.replace(old, new)))
    assert res["ask"] is False and res["reply"]["state"] == "declined" and why in res["reply"]["error"], res
    assert host.txn() == {} and "rekey.next" not in host.etc_files()


@pytest.mark.parametrize("old, new", [
    ("Endpoint = vps.example.org:51821", "Endpoint = vps2.example.org:51821"),
    ("AllowedIPs = 10.8.1.0/24, 10.99.99.0/30", "AllowedIPs = 10.8.1.0/24, 10.99.99.0/30, 192.168.70.0/24"),
    ("PersistentKeepalive = 25-35", "PersistentKeepalive = 25"),
])
def test_endpoint_allowed_ips_and_keepalive_of_the_invitation_do_not_matter(svc, host, old, new):
    """Endpoint, AllowedIPs и keepalive приглашения не сверяются: переключение
    берёт их из стоящего конфига (документ мог поменять их между стадией и
    приглашением) — отказывать из-за них значит не дать сменить ключи
    никогда после «✏️ Домен» или новой подсети соседа."""
    conf, _, _ = _next_conf(host)
    assert old in conf
    res = svc.rekey_next_in(_msg(conf.replace(old, new)))
    assert res["ask"] is True and res["reply"]["state"] == "asked", res


def test_the_same_private_key_is_declined(svc, host):
    """«Новые» ключи, совпадающие с живыми, — менять нечего: отказ."""
    res = svc.rekey_next_in(_msg(host.standing))
    assert res["reply"]["state"] == "declined" and "тот же" in res["reply"]["error"], res
    assert host.txn() == {}


def test_the_same_server_key_is_declined(svc, host):
    """Свой ключ новый, а ключ сервера прежний — переход наполовину: шлюз
    не поднял бы линк с ВПС, который сменил свою сторону. Отказ."""
    priv, _ = keypair()
    res = svc.rekey_next_in(_msg(gw_conf(priv, host.vps_pub, "DUMMY-NEW-PSK=")))
    assert res["reply"]["state"] == "declined" and "ключ сервера тот же" in res["reply"]["error"], res
    assert host.txn() == {}


def test_another_transition_while_switching_is_declined(svc, host, clock):
    """Посреди переключения другой id не перебивает текущий переход."""
    _switched(svc, host, clock)
    conf, _, _ = _next_conf(host)
    res = svc.rekey_next_in(_msg(conf, "ffff00001111"))
    assert res == {"reply": {"id": "ffff00001111", "state": "declined", "error": "идёт другая смена ключей"},
                   "ask": False, "replaced": None}
    assert host.txn()["id"] == "a1b2c3d4e5f6" and host.txn()["state"] == "switched"


def test_another_transition_during_the_search_is_declined(svc, host, clock):
    _switched(svc, host, clock)
    clock.at(gwrekey.SWITCH_SECONDS + 5)
    svc.rekey_tick()
    assert host.txn()["state"] == "back"
    conf, _, _ = _next_conf(host)
    assert svc.rekey_next_in(_msg(conf, "ffff00001111"))["reply"]["state"] == "declined"
    assert host.txn()["id"] == "a1b2c3d4e5f6"


def test_a_message_without_an_id_is_ignored(svc, host):
    conf, _, _ = _next_conf(host)
    assert svc.rekey_next_in({"t": "link_next", "id": "", "conf": conf}) == {"reply": None, "ask": False,
                                                                             "replaced": None}
    assert host.txn() == {}


# ── ответ человека и отзыв ───────────────────────────────────────────────────

def test_apply_makes_the_gateway_ready(svc, host):
    """«Применить» — ready (серверу link_ack ready), вопрос снят; без вопроса
    — ответа нет."""
    assert svc.rekey_answer(True) is None
    conf, _, _ = _next_conf(host)
    svc.rekey_next_in(_msg(conf))
    assert svc.rekey_answer(True) == {"id": "a1b2c3d4e5f6", "state": "ready", "error": ""}
    assert host.txn()["state"] == "ready" and "link" not in svc.cfg_questions()
    assert svc.rekey_answer(True) is None, "второй ответ на снятый вопрос"


def test_cancel_in_the_chat_answers_declined_and_keeps_only_a_mark(svc, host):
    """«Отмена»: серверу declined, конфиги с приватным ключом стёрты, остаётся
    метка отказа — сервер, не получивший ответа при лежащем канале, повторит
    тот же id, и шлюз ответит «отказ» без нового вопроса."""
    conf, _, _ = _next_conf(host)
    svc.rekey_next_in(_msg(conf))
    assert svc.rekey_answer(False) == {"id": "a1b2c3d4e5f6", "state": "declined", "error": ""}
    assert host.etc_files() == {"link.txn"}, host.etc_files()
    assert host.txn()["state"] == "declined" and host.txn()["id"] == "a1b2c3d4e5f6"
    assert host.installed() == host.standing
    again = svc.rekey_next_in(_msg(conf))
    assert again == {"reply": {"id": "a1b2c3d4e5f6", "state": "declined", "error": ""}, "ask": False,
                     "replaced": None}, "отказ забыт — человека спросили второй раз"
    assert svc.cfg_questions() == []


def test_a_new_id_after_a_refusal_is_asked_normally(svc, host):
    conf, _, _ = _next_conf(host)
    svc.rekey_next_in(_msg(conf))
    svc.rekey_answer(False)
    newer, _, _ = _next_conf(host)
    assert svc.rekey_next_in(_msg(newer, "cccc44445555"))["ask"] is True


def test_the_refusal_mark_lives_a_day(svc, host, clock):
    conf, _, _ = _next_conf(host)
    svc.rekey_next_in(_msg(conf))
    svc.rekey_answer(False)
    clock.at(gwrekey.DECLINED_KEEP - 60)
    assert svc.rekey_tick() == QUIET and host.txn()["state"] == "declined"
    clock.at(gwrekey.DECLINED_KEEP + 60)
    assert svc.rekey_tick() == QUIET and host.txn() == {} and host.etc_files() == set()


def test_link_cancel_withdraws_only_the_named_question(svc, host, clock):
    """link_cancel с id текущего — снято, сообщение приглашения отдаётся для
    правки; чужой id — нет; посреди переключения отзыв не принимается — линк
    уже на новых ключах."""
    conf, _, _ = _next_conf(host)
    svc.rekey_next_in(_msg(conf))
    svc.rekey_mark_msg(555, 42)
    assert svc.rekey_cancel({"id": "000000000000"}) is None and host.txn()["state"] == "asked"
    assert svc.rekey_cancel({"id": "a1b2c3d4e5f6"}) == {"msg": [555, 42]}
    assert host.txn() == {} and host.etc_files() == set()
    _switched(svc, host, clock, rid="b2b2b2b2b2b2")
    assert svc.rekey_cancel({"id": "b2b2b2b2b2b2"}) is None and host.txn()["state"] == "switched"


def test_link_cancel_of_a_refused_invitation_removes_the_mark_silently(svc, host):
    """Отказ уже был — сказать человеку нечего: метка снимается молча."""
    conf, _, _ = _next_conf(host)
    svc.rekey_next_in(_msg(conf))
    svc.rekey_answer(False)
    assert svc.rekey_cancel({"id": "a1b2c3d4e5f6"}) is None
    assert host.txn() == {} and host.etc_files() == set()
