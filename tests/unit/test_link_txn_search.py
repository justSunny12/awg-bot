"""
Смена ключей линка на шлюзе: domain/gateway/cfg/rekey.py (приглашение,
согласие, переключение, отсчёт 90 с, поиск рабочего конфига сутки, закрепление
по первому подписанному ответу сервера) и runtime/linkclient/rekey.py
(link_next, link_go, link_cancel, link_done) — и сквозной переход двух половин
в одном процессе, включая потерю подтверждения в момент переключения.

Подтверждение может потеряться ровно в момент переключения — таймерами это
не решить, поэтому решает сервер, а шлюз сутки держит второй конфиг и ищет.
Цена ошибки: шлюз, который не вернулся сам и не нашёл рабочий конфиг, —
мёртвый линк, мёртвый канал и РФ-доступ у всех до визита к шлюзу; вопрос в
чате агента, заданный второй раз после разрыва сессии; поиск, который не
кончается и дёргает линк каждые три минуты.

Хост — `_Host` из теста документа (юнит, конфиг линка, /etc/awg-gw, скрипт
обвязки в режиме --link-apply ставит файл на место установленного) с одной
надстройкой: хендшейк линка есть, только если установленный конфиг шлюза
составляет пару с живым конфигом ВПС (модель ВПС — `_Vps` из
test_gwlink_rekey.py) или если тест сказал «линк жив». Часы — одни на обе
стороны: `time.time` агента и `timeutil.now` сервера.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import time
import types

import pytest

from awgbot.domain import gwcfg
from awgbot.domain.gateway.cfg import rekey as gwrekey
from awgbot.runtime import linkclient
from awgbot.runtime import linkserver
from awgbot.runtime.linkserver import rekey as lsrekey
from awgbot.util import gwlink, timeutil, wgconf
from tests.unit import test_gw_cfg_apply as _apply
from tests.unit import test_gwlink_rekey as _vps
from tests.unit.test_gwlink_rekey import gw_conf, keypair

pytestmark = pytest.mark.unit

conf_dir = _apply.conf_dir
svc = _apply.svc
slot = _vps.slot
vps = _vps.vps


class _Clock:
    """Одни часы на агент и сервер: `time.time` (агент: отсчёт, поиск,
    возраст хендшейка) и `timeutil.now` (сервер: сроки перехода)."""

    def __init__(self, monkeypatch):
        self.t0 = time.time()
        self.shift = 0.0
        monkeypatch.setattr(time, "time", lambda: self.t0 + self.shift)
        monkeypatch.setattr(timeutil, "now",
                            lambda: datetime.datetime.fromtimestamp(self.t0 + self.shift, timeutil.TZ))

    def at(self, seconds: float) -> None:
        self.shift = float(seconds)

    @property
    def now(self) -> float:
        return self.t0 + self.shift


class _Host(_apply._Host):
    """Шлюз: всё из теста документа, плюс модель хендшейка линка. `peer` —
    модель ВПС (`_Vps`): хендшейк свежий, когда установленный конфиг шлюза и
    живой конфиг ВПС — одна пара ключей и сеть не порвана (`broken`). Без
    `peer` — по флагу `alive`."""

    def __init__(self, tmp_path, monkeypatch, clock):
        super().__init__(tmp_path, monkeypatch)
        self.clock = clock
        self.peer = None
        self.alive = True
        self.broken = False

    def link_up(self) -> bool:
        if self.broken:
            return False
        if self.peer is None:
            return self.alive
        mine = wgconf.parse(self.link.read_text(encoding="utf-8"))
        return (wgconf.pubkey_of(mine.get("Interface", "PrivateKey")) == self.peer.peer_pub()
                and mine.get("Peer", "PublicKey") == self.peer.live_pub())

    def _run(self, argv, **kw):
        argv = list(argv)
        if argv[:2] == ["awg", "show"] and argv[-1] == "latest-handshakes":
            self.calls.append(argv)
            stamp = int(self.clock.now) - 5 if self.link_up() else 0
            return _apply._cp(0, f"DUMMYPUB=\t{stamp}\n".encode())
        return super()._run(argv, **kw)

    def installed(self) -> str:
        return self.link.read_text(encoding="utf-8")

    def txn(self) -> dict:
        p = self.etc / "link.txn"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def etc_files(self) -> set[str]:
        return {f.name for f in self.etc.iterdir()} if self.etc.exists() else set()


@pytest.fixture()
def clock(monkeypatch):
    return _Clock(monkeypatch)


@pytest.fixture()
def host(tmp_path, monkeypatch, clock):
    """Шлюз со своей парой ключей линка (настоящий X25519 — по ней модель
    решает, жив ли линк)."""
    h = _Host(tmp_path, monkeypatch, clock)
    h.priv, h.pub = keypair()
    h.vps_priv, h.vps_pub = keypair()
    h.standing = gw_conf(h.priv, h.vps_pub, "DUMMY-LINK-PSK=")
    h.link.write_text(h.standing, encoding="utf-8")
    return h


def _next_conf(host, **over) -> tuple[str, str, str]:
    """Новые ключи шлюзовой стороны так, как их пришлёт сервер: (текст, свой
    публичный, публичный ВПС). `endpoint` — другой Endpoint (для отказа)."""
    priv, pub = keypair()
    _, vpub = keypair()
    return gw_conf(priv, vpub, "DUMMY-NEW-PSK=", **over), pub, vpub


def _msg(conf: str, rid: str = "a1b2c3d4e5f6") -> dict:
    return {"t": "link_next", "id": rid, "conf": conf, "fp": "ignored"}


def _ready(svc, host, rid: str = "a1b2c3d4e5f6") -> str:
    conf, _, _ = _next_conf(host)
    svc.rekey_next_in(_msg(conf, rid))
    svc.rekey_answer(True)
    return conf


def _switched(svc, host, clock, rid: str = "a1b2c3d4e5f6") -> str:
    conf = _ready(svc, host, rid)
    assert svc.rekey_go({"id": rid})["ok"] is True
    return conf


QUIET = {"note": "", "msg": None}
NONE_ANSWERED = {"done": None, "note": "", "msg": None}
BACK_NOTE = "⚠️ Новые ключи линка не поднялись — вернул прежние"


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


# ── переключение ─────────────────────────────────────────────────────────────

def test_link_go_keeps_the_old_conf_and_installs_the_new_one(svc, host, clock):
    """link_go: прежний конфиг — в rekey.prev (вернуться есть на что), новый
    ставится скриптом (--link-apply из rekey.next), состояние switched, и
    записано оно ДО применения: перезапуск посреди продолжит отсчёт."""
    conf = _ready(svc, host)
    svc.rekey_mark_msg(555, 42)
    res = svc.rekey_go({"id": "a1b2c3d4e5f6"})
    assert res["ok"] is True and res["error"] == "", res
    seen_state = [json.loads(seen["link.txn"][0])["state"] for seen in host.etc_seen]
    assert seen_state == ["switched"], f"состояние в момент применения: {seen_state}"
    assert host.link_applies == ["rekey.next"], host.link_applies
    assert gwcfg.link_print(host.installed()) == gwcfg.link_print(conf), "новый конфиг не встал"
    prev = (host.etc / "rekey.prev").read_text(encoding="utf-8")
    assert gwcfg.link_print(prev) == gwcfg.link_print(host.standing), "в rekey.prev не прежний конфиг"
    assert (host.etc / "rekey.prev").stat().st_mode & 0o077 == 0
    assert host.txn()["state"] == "switched" and host.txn()["switched_at"] == pytest.approx(clock.now)


def test_link_go_keeps_what_the_document_changed_since_the_invitation(svc, host):
    """Между приглашением и «Применить» документ сменил Endpoint и добавил
    подсеть соседа: переключение ставит НОВЫЕ ключи, PSK и обфускацию на
    СТОЯЩИЙ конфиг — Endpoint и AllowedIPs не откатываются к приглашению."""
    conf, pub, vpub = _next_conf(host)
    svc.rekey_next_in(_msg(conf))
    svc.rekey_answer(True)
    moved = (host.standing.replace("Endpoint = vps.example.org:51821", "Endpoint = vps2.example.org:51821")
             .replace("AllowedIPs = 10.8.1.0/24, 10.99.99.0/30", "AllowedIPs = 10.8.1.0/24, 10.99.99.0/30, 192.168.70.0/24"))
    host.link.write_text(moved, encoding="utf-8")
    assert svc.rekey_go({"id": "a1b2c3d4e5f6"})["ok"] is True
    now = wgconf.parse(host.installed())
    assert now.get("Peer", "Endpoint") == "vps2.example.org:51821", "Endpoint откатился к приглашению"
    assert "192.168.70.0/24" in now.get("Peer", "AllowedIPs"), "подсеть соседа пропала при смене ключей"
    assert wgconf.pubkey_of(now.get("Interface", "PrivateKey")) == pub and now.get("Peer", "PublicKey") == vpub
    assert now.get("Peer", "PresharedKey") == "DUMMY-NEW-PSK="


def test_link_go_without_consent_does_nothing(svc, host):
    """link_go без «Применить» (или с чужим id) — отказ, линк не тронут."""
    conf, _, _ = _next_conf(host)
    svc.rekey_next_in(_msg(conf))
    res = svc.rekey_go({"id": "a1b2c3d4e5f6"})
    assert res["ok"] is False and res.get("quiet") is True, res
    svc.rekey_answer(True)
    assert svc.rekey_go({"id": "ffffffffffff"}).get("quiet") is True
    assert host.link_runs == [] and host.installed() == host.standing


def test_link_go_refused_by_the_script_leaves_the_old_link(svc, host):
    """Скрипт обвязки отказал новому конфигу — переход снят (метка отказа:
    повтор того же id — отказ), живой конфиг прежний, конфигов с ключами нет,
    причина — последняя строка вывода (уйдёт серверу declined), сообщение
    приглашения — для итога."""
    _ready(svc, host)
    host.txn()                                            # ready
    st = host.txn(); st["msg"] = [555, 42]
    (host.etc / "link.txn").write_text(json.dumps(st), encoding="utf-8")
    host.link_rc = 1
    res = svc.rekey_go({"id": "a1b2c3d4e5f6"})
    assert res["ok"] is False and "не прошёл проверку" in res["error"] and res["msg"] == [555, 42], res
    assert host.installed() == host.standing
    assert host.etc_files() == {"link.txn"} and host.txn()["state"] == "declined", host.etc_files()


# ── отсчёт и поиск ───────────────────────────────────────────────────────────

def test_no_server_answer_within_90_seconds_goes_back_once(svc, host, clock):
    """90 с после переключения без подписанного ответа сервера — прежний
    конфиг на место, состояние back (поиск), человеку — одно уведомление
    правкой сообщения приглашения."""
    _ready(svc, host)
    st = host.txn(); st["msg"] = [555, 42]
    (host.etc / "link.txn").write_text(json.dumps(st), encoding="utf-8")
    svc.rekey_go({"id": "a1b2c3d4e5f6"})
    clock.at(gwrekey.SWITCH_SECONDS - 5)
    assert svc.rekey_tick() == QUIET and host.txn()["state"] == "switched"
    clock.at(gwrekey.SWITCH_SECONDS + 5)
    assert svc.rekey_tick() == {"note": BACK_NOTE, "msg": [555, 42]}
    assert host.installed() == gwcfg_render(host.standing), "прежний конфиг не вернулся"
    st = host.txn()
    assert st["state"] == "back" and st["current"] == "prev" and st["interval"] == gwrekey.SEARCH_FIRST
    assert {"rekey.next", "rekey.prev"} <= host.etc_files(), "второй конфиг для поиска не сохранён"
    assert svc.rekey_tick() == QUIET, "уведомление о возврате повторилось"


def gwcfg_render(text: str) -> str:
    """Конфиг так, как его кладёт агент: канонический текст со штампом."""
    return wgconf.render(wgconf.parse(text), "link", stamp="1")


def _tries(svc, host, clock, until: float, step: float = 30) -> list[float]:
    """Тикать каждые `step` с до `until`; вернуть моменты (от начала часов),
    когда агент переставлял конфиг линка."""
    out = []
    while clock.shift < until:
        clock.at(clock.shift + step)
        before = len(host.link_runs)
        svc.rekey_tick()
        if len(host.link_runs) > before:
            out.append(clock.shift)
    return out


def test_search_doubles_its_step_up_to_half_an_hour(svc, host, clock):
    """Линк молчит на обоих конфигах: шлюз пробует второй через 3 минуты,
    затем шаг удваивается — 6, 12, 24 минуты — и упирается в 30 (плюс
    подтверждение смерти линка вторым замером). Ровный шаг без роста дёргал
    бы линк каждые 3 минуты сутки напролёт."""
    _switched(svc, host, clock)
    host.alive = False
    clock.at(100)
    svc.rekey_tick()                                      # возврат на прежний
    runs = len(host.link_runs)
    tries = _tries(svc, host, clock, until=100 + 3 * 3600)
    gaps = [b - a for a, b in zip([100.0] + tries, tries)]
    want = [180, 360, 720, 1440, 1800, 1800]
    assert len(gaps) >= len(want), gaps
    for got, exp in zip(gaps, want):
        assert exp <= got <= exp + 2 * gwrekey.DEAD_CONFIRM_SECONDS, f"шаги поиска {gaps}, ждали {want}…"
    names = [n for n in host.link_applies[runs:]]
    assert names[:4] == ["rekey.next", "rekey.prev", "rekey.next", "rekey.prev"], names


def test_a_single_dead_reading_does_not_switch_the_link(svc, host, clock):
    """Один замер «мёртв» — ещё не повод переставлять линк: нужен второй не
    раньше чем через 30 с. Разовый пропуск хендшейка иначе рвал бы живой линк."""
    _switched(svc, host, clock)
    host.alive = False
    clock.at(100)
    svc.rekey_tick()
    runs = len(host.link_runs)
    clock.at(100 + gwrekey.SEARCH_FIRST + 1)
    svc.rekey_tick()
    assert len(host.link_runs) == runs, "переставили по одному замеру"
    clock.at(100 + gwrekey.SEARCH_FIRST + 1 + gwrekey.DEAD_CONFIRM_SECONDS - 5)
    svc.rekey_tick()
    assert len(host.link_runs) == runs, "второй замер ближе 30 с засчитан"
    clock.at(100 + gwrekey.SEARCH_FIRST + 1 + gwrekey.DEAD_CONFIRM_SECONDS)
    svc.rekey_tick()
    assert host.link_applies[runs:] == ["rekey.next"]


def test_an_unanswering_awg_show_decides_nothing(svc, host, clock, monkeypatch):
    """`awg show` не ответил — «не знаю»: поиск линк не трогает."""
    from awgbot.infra import gwguard
    _switched(svc, host, clock)
    clock.at(100)
    svc.rekey_tick()
    monkeypatch.setattr(gwguard, "handshake_state", lambda iface: ("unknown", None))
    runs = len(host.link_runs)
    _tries(svc, host, clock, until=3 * 3600, step=60)
    assert len(host.link_runs) == runs, host.link_applies[runs:]


def test_a_link_with_an_old_handshake_counts_as_dead(svc, host, clock, monkeypatch):
    """Хендшейк есть, но старше app.gateway.handshake_max_age — линк мёртв
    тем же признаком, что у монитора; моложе — жив."""
    from awgbot.infra import gwguard
    _switched(svc, host, clock)
    clock.at(100)
    svc.rekey_tick()
    age = {"v": 299}
    monkeypatch.setattr(gwguard, "handshake_state", lambda iface: ("ok", age["v"]))
    runs = len(host.link_runs)
    _tries(svc, host, clock, until=100 + gwrekey.SEARCH_FIRST + 120, step=30)
    assert len(host.link_runs) == runs, "живой по возрасту хендшейка линк переставили"
    age["v"] = 301
    _tries(svc, host, clock, until=clock.shift + 90, step=30)
    assert len(host.link_runs) == runs + 1


def test_search_stops_switching_once_the_link_is_alive(svc, host, clock):
    """Второй конфиг ожил — дальше линк не трогается, хоть до конца суток."""
    _switched(svc, host, clock)
    host.alive = False
    clock.at(100)
    svc.rekey_tick()
    _tries(svc, host, clock, until=100 + gwrekey.SEARCH_FIRST + 2 * gwrekey.DEAD_CONFIRM_SECONDS)
    assert host.txn()["current"] == "next", "модель: поиск не дошёл до нового конфига"
    host.alive = True
    runs = len(host.link_runs)
    _tries(svc, host, clock, until=6 * 3600, step=120)
    assert len(host.link_runs) == runs, f"живой линк переставляли: {host.link_applies[runs:]}"
    assert host.txn()["current"] == "next"


def test_after_a_day_the_search_ends_on_a_live_link_and_removes_the_spare_conf(svc, host, clock):
    """Сутки поиска вышли, линк жив — второй конфиг (с приватным ключом)
    удалён, состояние снято, линк больше не переставляется."""
    _switched(svc, host, clock)
    clock.at(100)
    svc.rekey_tick()
    assert host.txn()["state"] == "back"
    clock.at(100 + gwrekey.SEARCH_SECONDS + 1)
    assert svc.rekey_tick() == QUIET
    assert host.txn() == {} and host.etc_files() == set(), host.etc_files()
    assert svc.rekey_tick_every() == 0


def test_after_a_day_a_dead_link_is_still_searched(svc, host, clock):
    """Сутки вышли, а линк мёртв на обоих конфигах — поиск не бросает:
    закрыться сейчас значит оставить шлюз на случайном из двух навсегда.
    Шаг — не чаще 30 минут."""
    _switched(svc, host, clock)
    host.alive = False
    clock.at(100)
    svc.rekey_tick()
    clock.at(100 + gwrekey.SEARCH_SECONDS - 30)
    svc.rekey_tick()                                      # такт идёт и в последние секунды суток
    tries = _tries(svc, host, clock, until=100 + gwrekey.SEARCH_SECONDS + 3 * 3600, step=60)
    assert host.txn()["state"] == "back" and {"rekey.next", "rekey.prev"} <= host.etc_files()
    assert len(tries) >= 2, f"поиск после суток остановился: {tries}"
    gaps = [b - a for a, b in zip(tries, tries[1:])]
    assert gaps == sorted(gaps) and max(gaps) <= gwrekey.SEARCH_MAX_INTERVAL + 2 * gwrekey.DEAD_CONFIRM_SECONDS, gaps


def test_the_first_dead_reading_after_a_day_does_not_close_the_search(svc, host, clock):
    """Сутки вышли ровно на такте, где замер «мёртв» первый после смены
    конфига (второго, подтверждающего, ещё нет): это не «линк жив» —
    закрываться нельзя, иначе шлюз остаётся на мёртвом конфиге без второго."""
    _switched(svc, host, clock)
    host.alive = False
    clock.at(100)
    svc.rekey_tick()                                      # back: замеры начинаются заново
    clock.at(100 + gwrekey.SEARCH_SECONDS + 1)
    svc.rekey_tick()
    assert host.txn().get("state") == "back", "поиск закрыт на первом же мёртвом замере после суток"
    assert {"rekey.next", "rekey.prev"} <= host.etc_files()


def test_an_unanswered_invitation_expires_after_a_day(svc, host, clock):
    """Приглашение или согласие без link_go сутки — снято, файлы стёрты, а
    приглашение правится на «отозван» (шлюз мог не выходить на связь, и
    link_cancel от сервера не найдёт состояния — кнопки висели бы вечно)."""
    conf, _, _ = _next_conf(host)
    svc.rekey_next_in(_msg(conf))
    svc.rekey_mark_msg(7, 42)
    clock.at(gwrekey.SEARCH_SECONDS + 10)
    assert svc.rekey_tick() == {"note": "Запрос отозван сервером AWG", "msg": [7, 42]}
    assert host.txn() == {} and host.etc_files() == set()


def test_consent_without_link_go_expires_after_a_day_on_a_live_link(svc, host, clock):
    _ready(svc, host)
    clock.at(gwrekey.SEARCH_SECONDS - 10)
    svc.rekey_tick()
    assert host.txn()["state"] == "ready"
    clock.at(gwrekey.SEARCH_SECONDS + 10)
    assert svc.rekey_tick()["note"] == "Запрос отозван сервером AWG"
    assert host.txn() == {} and host.etc_files() == set()
    assert host.link_runs == [], "живой линк трогали без link_go"


def test_consent_without_link_go_never_searches_even_on_a_dead_link(svc, host, clock):
    """Из ready поиска нет: ВПС на новых ключах бывает только 180 с после
    link_go и возвращается сам раньше, чем шлюз заметил бы мёртвый линк;
    поиск отсюда увёл бы шлюз на ключи, которых у ВПС уже нет. ready снимает
    link_cancel сервера или сутки."""
    _ready(svc, host)
    host.alive = False
    _tries(svc, host, clock, until=6 * 3600, step=30)
    assert host.link_runs == [] and host.txn()["state"] == "ready", host.link_applies


def test_link_cancel_removes_consent(svc, host):
    """Сервер вернул прежние и прислал link_cancel — согласие снято, конфиг
    с новым ключом стёрт, человеку — правка приглашения."""
    _ready(svc, host)
    st = host.txn(); st["msg"] = [555, 42]
    (host.etc / "link.txn").write_text(json.dumps(st), encoding="utf-8")
    assert svc.rekey_cancel({"id": "a1b2c3d4e5f6"}) == {"msg": [555, 42]}
    assert host.txn() == {} and host.etc_files() == set()


def test_consent_with_a_live_link_never_touches_it(svc, host, clock):
    _ready(svc, host)
    _tries(svc, host, clock, until=6 * 3600, step=60)
    assert host.link_runs == [] and host.txn()["state"] == "ready"


def test_the_fast_tick_period_follows_the_state(svc, host, clock):
    """Частый такт: 10 с, пока ждём ответа сервера после переключения
    (срок 90 с), 30 с в поиске; в asked, ready, declined и вне перехода — 0
    (хватит такта монитора)."""
    assert svc.rekey_tick_every() == 0
    conf, _, _ = _next_conf(host)
    svc.rekey_next_in(_msg(conf))
    assert svc.rekey_tick_every() == 0
    svc.rekey_answer(True)
    assert svc.rekey_tick_every() == 0
    svc.rekey_go({"id": "a1b2c3d4e5f6"})
    assert svc.rekey_tick_every() == gwrekey.TICK_SWITCHED == 10
    clock.at(gwrekey.SWITCH_SECONDS + 5)
    svc.rekey_tick()
    assert svc.rekey_tick_every() == gwrekey.TICK_SEARCH == 30
    svc.rekey_server_answered()
    assert svc.rekey_tick_every() == 0


# ── ответ сервера закрепляет выбор ───────────────────────────────────────────

def test_a_server_answer_after_the_switch_fixes_the_new_keys(svc, host, clock):
    """Первое подписанное слово сервера в новой сессии на новых ключах:
    link_done ok, «✅ Линк на новых ключах» правкой приглашения, прежний
    конфиг стёрт."""
    _ready(svc, host)
    st = host.txn(); st["msg"] = [555, 42]
    (host.etc / "link.txn").write_text(json.dumps(st), encoding="utf-8")
    svc.rekey_go({"id": "a1b2c3d4e5f6"})
    res = svc.rekey_server_answered()
    assert res == {"done": {"id": "a1b2c3d4e5f6", "ok": True, "keys": "new"}, "note": "✅ Линк на новых ключах",
                   "msg": [555, 42]}
    # файлы перехода стёрты; осталась метка выпуска — момент смены ключей:
    # файл конфигурации, выпущенный раньше, несёт прежние ключи и получит отказ
    assert host.txn() == {} and host.etc_files() == {"issued_at"}
    assert int((host.etc / "issued_at").read_text(encoding="utf-8")) == int(clock.now)
    assert svc.bundle_issued_applied() == int(clock.now)
    assert svc.rekey_server_answered() == NONE_ANSWERED, "итог повторился на следующей сессии"


def test_a_server_answer_during_the_search_names_the_keys_that_work(svc, host, clock):
    """В поиске: ответ пришёл на новых (current next) — «новые», с
    уведомлением; на прежних — link_done keys old без уведомления (о
    возврате человек уже знает)."""
    _switched(svc, host, clock)
    clock.at(100)
    svc.rekey_tick()                                      # back, current prev
    res = svc.rekey_server_answered()
    assert res == {"done": {"id": "a1b2c3d4e5f6", "ok": False, "keys": "old"}, "note": "", "msg": None}, res
    assert host.txn() == {} and host.etc_files() == set()

    _switched(svc, host, clock, rid="c3c3c3c3c3c3")
    clock.at(300)
    host.alive = False
    svc.rekey_tick()                                      # back
    _tries(svc, host, clock, until=300 + gwrekey.SEARCH_FIRST + 2 * gwrekey.DEAD_CONFIRM_SECONDS)
    assert host.txn()["current"] == "next"
    res = svc.rekey_server_answered()
    assert res["done"] == {"id": "c3c3c3c3c3c3", "ok": True, "keys": "new"} and res["note"] == "✅ Линк на новых ключах"


def test_the_live_config_not_the_record_decides_which_keys_work(svc, host, clock):
    """Запись говорит «переключились», а на линке стоит прежний конфиг
    (сбой посреди применения): первое слово сервера закрепляет ПРЕЖНИЕ —
    «✅ новые» здесь было бы ложью, а link_done keys new увёл бы ВПС."""
    _switched(svc, host, clock)
    host.link.write_text((host.etc / "rekey.prev").read_text(encoding="utf-8"), encoding="utf-8")
    res = svc.rekey_server_answered()
    assert res["done"]["keys"] == "old" and res["done"]["ok"] is False and res["note"] == "", res
    assert host.txn() == {} and host.etc_files() == set()


def test_a_busy_transition_lock_skips_the_tick(svc, host, clock):
    """Идёт link_go или ответ сервера (замок перехода у другого потока) —
    такт не ждёт и ничего не решает: иначе он вернул бы прежние посреди
    переключения."""
    import threading
    _switched(svc, host, clock)
    clock.at(gwrekey.SWITCH_SECONDS + 5)
    held, release = threading.Event(), threading.Event()

    def holder():
        with gwrekey._RK_LOCK:
            held.set()
            release.wait(5)
    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    try:
        assert svc.rekey_tick() == QUIET
        assert host.txn()["state"] == "switched" and len(host.link_runs) == 1
    finally:
        release.set()
        t.join(5)
    assert svc.rekey_tick()["note"] == BACK_NOTE, "после освобождения замка срок не сработал"


def test_a_failed_search_step_waits_a_whole_interval(svc, host, clock):
    """Перестановка в поиске не встала (скрипт отказал) — следующая попытка не
    на ближайшем такте, а через шаг: линк не дёргают каждые 30 с."""
    _switched(svc, host, clock)
    host.alive = False
    clock.at(100)
    svc.rekey_tick()
    host.link_rc = 1
    tries = _tries(svc, host, clock, until=100 + gwrekey.SEARCH_FIRST * 3, step=30)
    gaps = [b - a for a, b in zip(tries, tries[1:])]
    assert len(tries) >= 2 and all(g >= gwrekey.SEARCH_FIRST for g in gaps), f"попытки после отказа: {tries}"


def test_a_server_answer_without_a_transition_says_nothing(svc, host):
    assert svc.rekey_server_answered() == NONE_ANSWERED
    conf, _, _ = _next_conf(host)
    svc.rekey_next_in(_msg(conf))
    assert svc.rekey_server_answered() == NONE_ANSWERED, "вопрос висит — закреплять нечего"
    assert host.txn()["state"] == "asked"
    svc.rekey_answer(True)
    assert svc.rekey_server_answered() == NONE_ANSWERED, "согласие без link_go закреплено как итог"
    assert host.txn()["state"] == "ready"


# ── клиент канала ────────────────────────────────────────────────────────────

class _Client:
    """Клиент канала без сокета: `_send` записывает, что ушло серверу;
    крючки вопроса, уведомления и правки сообщения — пакета; `closed` —
    сколько раз клиент сам закрыл свой writer; `kicks` — запуски частого такта."""

    def __init__(self, svc, monkeypatch):
        self.client = linkclient.LinkClient(svc)
        self.closed = 0

        def close():
            self.closed += 1
        self.client._writer = types.SimpleNamespace(close=close)
        self.sent: list[tuple[str, dict]] = []
        self.asks: list[str] = []
        self.notes: list[str] = []
        self.edits: list[tuple[int, int, str]] = []
        self.edit_ok = True
        self.kicks = 0
        self.on_send = None

        async def send(kind, body=None, pad=0):
            self.sent.append((kind, dict(body or {})))
            if self.on_send is not None:
                return await self.on_send(kind, dict(body or {}))
            return True
        self.client._send = send

        async def ask(section):
            self.asks.append(section)

        async def notify(text):
            self.notes.append(text)

        async def edit(chat, mid, text):
            self.edits.append((chat, mid, text))
            return self.edit_ok

        def kick():
            self.kicks += 1
        self.client.rekey_kick = kick
        monkeypatch.setattr(linkclient, "_ask", ask)
        monkeypatch.setattr(linkclient, "_notify", notify)
        monkeypatch.setattr(linkclient, "_edit", edit)
        monkeypatch.setattr(linkclient, "_client", self.client)


async def test_link_next_is_acknowledged_and_asked_once(svc, host, monkeypatch):
    """link_next → link_ack asked и приглашение в чате; тот же link_next снова
    (новая сессия) → link_ack, но приглашения второй раз нет."""
    c = _Client(svc, monkeypatch)
    conf, _, _ = _next_conf(host)
    await c.client._handle(_msg(conf))
    assert c.sent == [("link_ack", {"id": "a1b2c3d4e5f6", "state": "asked", "error": ""})]
    assert c.asks == ["link"]
    await c.client._handle(_msg(conf))
    assert len(c.sent) == 2 and c.asks == ["link"], "приглашение задано второй раз"


async def test_a_replaced_invitation_is_edited_to_say_so(svc, host, monkeypatch):
    """Новое приглашение поверх висящего: прежнее сообщение правится —
    «Запрос заменён более новым», кнопки старого не ведут в никуда."""
    c = _Client(svc, monkeypatch)
    conf, _, _ = _next_conf(host)
    await c.client._handle(_msg(conf))
    svc.rekey_mark_msg(555, 42)
    newer, _, _ = _next_conf(host)
    await c.client._handle(_msg(newer, "dddd66667777"))
    assert c.edits == [(555, 42, "Запрос заменён более новым")] and c.notes == [], (c.edits, c.notes)
    assert c.asks == ["link", "link"]


async def test_the_answer_in_the_chat_reaches_the_server(svc, host, monkeypatch):
    """«Применить» в чате агента: link_ack ready уходит серверу сразу; частый
    такт не нужен — в ready сроков короче суток нет."""
    c = _Client(svc, monkeypatch)
    conf, _, _ = _next_conf(host)
    await c.client._handle(_msg(conf))
    reply = await linkclient.report_rekey_answer(svc, True)
    assert reply["state"] == "ready" and c.sent[-1] == ("link_ack", reply)


async def test_a_successful_link_go_closes_the_session_to_come_back_on_new_keys(svc, host, monkeypatch):
    """link_go встал: клиент сам закрывает сессию — TCP по линку пережил бы
    down/up, а фиксация на обеих сторонах ждёт новой сессии новым ключом;
    частый такт включён; серверу на старой сессии ничего не уходит."""
    c = _Client(svc, monkeypatch)
    _ready(svc, host)
    await c.client._handle({"t": "link_go", "id": "a1b2c3d4e5f6"})
    assert c.closed == 1 and c.kicks == 1, (c.closed, c.kicks)
    assert c.sent == [] and host.txn()["state"] == "switched"


async def test_a_failed_link_go_is_reported_as_declined_and_the_human_told(svc, host, monkeypatch):
    """link_go, который скрипт не поставил, — link_ack declined с причиной
    (сервер сразу вернёт прежние, а не через 3 минуты), сессия не рвётся,
    человеку — «вернул прежние» правкой приглашения."""
    c = _Client(svc, monkeypatch)
    _ready(svc, host)
    st = host.txn(); st["msg"] = [555, 42]
    (host.etc / "link.txn").write_text(json.dumps(st), encoding="utf-8")
    host.link_rc = 1
    await c.client._handle({"t": "link_go", "id": "a1b2c3d4e5f6"})
    kind, body = c.sent[-1]
    assert kind == "link_ack" and body["state"] == "declined" and body["id"] == "a1b2c3d4e5f6"
    assert "не прошёл проверку" in body["error"], body
    assert c.edits == [(555, 42, BACK_NOTE)] and c.closed == 0, (c.edits, c.closed)


async def test_link_cancel_tells_the_human_once(svc, host, monkeypatch):
    """Отзыв сервером — правка приглашения «Запрос отозван сервером AWG»;
    сообщения не знаем или правка не прошла — отдельное сообщение; второй
    отзыв того же id — тишина."""
    c = _Client(svc, monkeypatch)
    conf, _, _ = _next_conf(host)
    await c.client._handle(_msg(conf))
    await c.client._handle({"t": "link_cancel", "id": "a1b2c3d4e5f6"})
    await c.client._handle({"t": "link_cancel", "id": "a1b2c3d4e5f6"})
    assert c.notes == ["Запрос отозван сервером AWG"] and c.edits == [], (c.notes, c.edits)
    newer, _, _ = _next_conf(host)
    await c.client._handle(_msg(newer, "eeee88889999"))
    svc.rekey_mark_msg(555, 43)
    c.edit_ok = False
    await c.client._handle({"t": "link_cancel", "id": "eeee88889999"})
    assert c.edits == [(555, 43, "Запрос отозван сервером AWG")]
    assert c.notes == ["Запрос отозван сервером AWG"] * 2, "правка не прошла — человек не узнал"


async def test_the_first_server_word_after_the_switch_sends_link_done(svc, host, clock, monkeypatch):
    c = _Client(svc, monkeypatch)
    _switched(svc, host, clock)
    await c.client._rekey_answered()
    assert c.sent == [("link_done", {"id": "a1b2c3d4e5f6", "ok": True, "keys": "new"})]
    assert c.notes == ["✅ Линк на новых ключах"]


async def test_the_fast_tick_runs_while_the_transition_lives_and_stops_after(svc, host, monkeypatch):
    """Частый такт: тикает с периодом состояния, пока переход идёт, и сам
    заканчивается — вечная задача на каждом шлюзе ни к чему; второй запуск
    поверх идущего не создаёт второго цикла."""
    client = linkclient.LinkClient(svc)
    every = iter([0.001, 0.001, 0])
    monkeypatch.setattr(svc, "rekey_tick_every", lambda: next(every))
    ticks = []
    monkeypatch.setattr(svc, "rekey_tick", lambda: ticks.append(1) or QUIET)
    client.rekey_kick()
    task = client.__dict__["_rekey_task"]
    client.rekey_kick()
    assert client.__dict__["_rekey_task"] is task, "второй частый такт поверх идущего"
    await asyncio.wait_for(task, 2)
    assert ticks == [1, 1], ticks


async def test_the_monitor_tick_steps_aside_while_the_fast_tick_runs(svc, host, monkeypatch):
    """Такт монитора при живом цикле перехода пропускается — два такта в ту
    же секунду дали бы двойное «вернул прежние»; цикла нет — тикает сам и
    поднимает цикл."""
    client = linkclient.LinkClient(svc)
    ticks = []
    monkeypatch.setattr(svc, "rekey_tick", lambda: ticks.append(1) or QUIET)
    monkeypatch.setattr(svc, "rekey_tick_every", lambda: 3600)
    client.rekey_kick()
    await asyncio.sleep(0)
    await client.rekey_tick_monitor()
    assert ticks == [], "монитор тикнул поверх цикла перехода"
    client.rekey_stop()
    await asyncio.sleep(0)
    assert "_rekey_task" not in client.__dict__
    await client.rekey_tick_monitor()
    assert ticks == [1] and not client.__dict__["_rekey_task"].done(), "монитор не поднял цикл"
    client.rekey_stop()


async def test_a_stale_link_go_answers_declined_without_bothering_the_human(svc, host, monkeypatch):
    """link_go без согласия или с чужим id (устаревший): серверу declined,
    человеку — ничего: ничего не трогали."""
    c = _Client(svc, monkeypatch)
    await c.client._handle({"t": "link_go", "id": "a1b2c3d4e5f6"})
    assert c.sent and c.sent[-1][1]["state"] == "declined", c.sent
    assert c.notes == [] and c.edits == [], (c.notes, c.edits)


async def test_the_uplink_tick_reports_the_answer_and_tells_the_human(svc, monkeypatch):
    """Такт аплинка клиента: ответ аварийного применения — серверу тем же
    путём, что ответ человека (очередь ответов), заметки — в чат по одной."""
    client = linkclient.LinkClient(svc)
    answer = {"ok": True, "section": "uplink", "accepted": True, "gen": 3, "hash": "ab", "print": "p"}
    monkeypatch.setattr(svc, "cfg_uplink_tick", lambda: {"answer": answer, "notes": ["раз", "два"]})
    reported, notes = [], []

    async def report(services, ans):
        reported.append(ans)

    async def notify(text):
        notes.append(text)
    monkeypatch.setattr(linkclient, "report_cfg_answer", report)
    monkeypatch.setattr(linkclient, "_notify", notify)
    await client.uplink_tick()
    assert reported == [answer] and notes == ["раз", "два"]
    monkeypatch.setattr(svc, "cfg_uplink_tick", lambda: {"answer": None, "notes": []})
    await client.uplink_tick()
    assert reported == [answer] and notes == ["раз", "два"], "пустой такт что-то отправил"


# ── сквозной переход: сервер и шлюз в одном процессе ─────────────────────────

class _Pair:
    """Две половины канала без сокетов: слушатель ВПС (`LinkServer` над
    настоящими Services и моделью ВПС) и клиент шлюза (`LinkClient` над
    настоящим агентом). Сообщение доходит, только если линк жив (пара ключей
    сходится и сеть не порвана); `lost` — виды сообщений сервера, теряемые в
    пути после сдачи в сокет; `unsent` — не ушедшие вовсе (send вернул
    False). Сессия открывается заново по `session()`: hello на сервере
    (может закрыть её молча), первое слово сервера у клиента, доставка
    перехода. `drops` — сколько раз сервер сам порвал сессию."""

    def __init__(self, services, svc, host, vps, monkeypatch):
        self.services, self.svc, self.host, self.vps = services, svc, host, vps
        host.peer = vps
        # шлюз стоит ровно на том, что знает ВПС: /root/gw-awglink.conf
        host.link.write_text(gwcfg_render(vps.gw.read_text(encoding="utf-8")), encoding="utf-8")
        self.server = linkserver.LinkServer(services)
        self.c = _Client(svc, monkeypatch)
        self.lost: set[str] = set()
        self.unsent: set[str] = set()
        self.server_notes: list[str] = []
        self.to_gateway: list[str] = []
        self.session_keys: tuple[str, str] = ("", "")
        self.drops = 0
        self.holds = 0
        self.serve_ticks = True

        async def server_send(slot_id, kind, body=None, pad=gwlink.PAD_DELTA):
            self.to_gateway.append(kind)
            if kind in self.unsent or not host.link_up():
                return False
            if kind in self.lost:
                return True                               # ушёл в сокет и пропал в пути
            await self.c.client._handle({"t": kind, **dict(body or {})})
            return True
        self.server.send = server_send

        async def client_send(kind, body):
            if not host.link_up():
                return False
            if kind == "link_ack":
                await self.server.rekey_ack_in(1, {"t": kind, **body})
            elif kind == "link_done":
                await self.server.rekey_done_in(1, {"t": kind, **body})
            return True
        self.c.on_send = client_send

        async def drop(slot_id, *, record=False):
            self.drops += 1
            self.session_keys = ("", "")
            self.server._sessions.pop(slot_id, None)
        self.server._drop = drop

        async def on_notes(notes):
            self.server_notes += [n.text for n in notes]
        monkeypatch.setattr(linkserver, "_on_notes", on_notes)
        monkeypatch.setattr(lsrekey, "COMMIT_DELAY", 0)

    async def session(self) -> bool:
        """Переподключение клиента: удаётся, только если линк жив и сервер не
        закрыл сессию на hello."""
        if not self.host.link_up():
            return False
        self.server._sessions[1] = types.SimpleNamespace(hello=True, caps=["cfg1", "confirm1"], rekey_sent="")
        if not await self.server.rekey_hello(1):
            self.holds += 1
            self.server._sessions.pop(1, None)
            return False
        self.session_keys = self._keys()
        await self.c.client._rekey_answered()
        await self.server.deliver_rekey(1)
        return True

    def _keys(self) -> tuple[str, str]:
        mine = wgconf.parse(self.host.installed()).get("Interface", "PrivateKey")
        return mine, self.vps.peer_pub()

    async def tick(self) -> None:
        """Такт обеих сторон; линк жив на ключах, на которых сессии ещё не
        было, — клиент переподключается (как его бэкофф после разрыва)."""
        steps = ([self.server.rekey_tick] if self.serve_ticks else []) + [self.c.client.rekey_tick]
        for step in steps:
            if not self.host.link_up():
                self.session_keys = ("", "")              # линк лёг — сессия умерла
            await step()
        if not self.host.link_up():
            self.session_keys = ("", "")
        elif self._keys() != self.session_keys:
            await self.session()

    async def run_until(self, clock, until: float, step: float = 10) -> None:
        while clock.shift < until:
            clock.at(clock.shift + step)
            await self.tick()

    def on_same_keys(self) -> bool:
        return self.host.link_up()


async def _settle(cond, timeout: float = 3.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


async def _to_ready(pair: _Pair) -> str:
    """Старт слушателя (возврат на старте отработал до перехода), стадия на
    ВПС, link_next шлюзу, «Применить» в чате агента."""
    await pair.server.rekey_boot()
    st = pair.services.gateway_rekey_stage(1)
    assert await pair.session()
    assert pair.services.gateway_rekey(1)["state"] == "asked", "шлюз не ответил asked"
    assert pair.c.asks == ["link"]
    await linkclient.report_rekey_answer(pair.svc, True)
    return st["id"]


async def _to_switch(pair: _Pair) -> str:
    rid = await _to_ready(pair)
    assert await _settle(lambda: pair.services.gateway_rekey(1).get("committed")), "ВПС не переключился"
    return rid


async def test_a_clean_transition_ends_on_new_keys_on_both_sides(services, slot, vps, svc, host, clock, monkeypatch):
    """Норма: link_go дошёл, шлюз переключился и сам закрыл сессию, ВПС
    переключился и порвал свою; hello новым ключом фиксирует переход на ВПС,
    первое слово сервера — на шлюзе; по одному уведомлению с каждой стороны,
    хвостов на дисках нет."""
    pair = _Pair(services, svc, host, vps, monkeypatch)
    old_gw_pub = vps.gw_pub
    await _to_switch(pair)
    assert pair.c.closed == 1, "шлюз не закрыл сессию после link_go"
    assert await _settle(lambda: pair.drops == 1), "ВПС не порвал сессию после переключения"
    assert pair.on_same_keys(), "после link_go стороны на разных ключах"
    assert await pair.session()
    assert vps.peer_pub() != old_gw_pub and vps.extra_files() == set()
    assert pair.server_notes == [pair.server_notes[0]] and "ключи линка сменены" in pair.server_notes[0]
    assert pair.c.notes == ["✅ Линк на новых ключах"]
    assert services.gateway_rekey(1) == {} and host.txn() == {} and host.etc_files() == {"issued_at"}


async def test_a_link_go_that_did_not_leave_is_repeated_in_the_next_session(services, slot, vps, svc, host, clock,
                                                                           monkeypatch):
    """link_go не ушёл (сессия умерла в этот момент): ВПС не переключается.
    Новая сессия повторяет link_next с тем же id, шлюз отвечает ready без
    второго вопроса — и переход доходит до новых ключей."""
    pair = _Pair(services, svc, host, vps, monkeypatch)
    pair.unsent = {"link_go"}
    await _to_ready(pair)
    await asyncio.sleep(0.05)
    assert services.gateway_rekey(1)["state"] == "asked" and "--rekey-commit" not in vps.modes
    assert host.txn()["state"] == "ready" and pair.on_same_keys()
    pair.unsent = set()
    assert await pair.session()
    assert await _settle(lambda: services.gateway_rekey(1).get("committed")), "переход не дошёл в новой сессии"
    assert pair.c.asks == ["link"], "человека спросили второй раз"
    assert await pair.session()
    assert pair.c.notes == ["✅ Линк на новых ключах"] and services.gateway_rekey(1) == {}


async def test_a_lost_link_go_ends_on_the_old_keys_on_both_sides(services, slot, vps, svc, host, clock, monkeypatch):
    """link_go ушёл в сокет и пропал: ВПС переключился, шлюз — нет (ready).
    Линк лежит, пока ВПС по сроку (180 с) не вернёт прежние; шлюз из ready
    линк не трогает. Линк оживает сам, при первой же сессии ВПС шлёт
    link_cancel — согласие на шлюзе снято, приглашение поправлено «отозван»;
    обе стороны на прежних, админу — одно «вернул прежние», агенту — ни
    одного ложного «✅», хвостов нет ни на одной стороне."""
    pair = _Pair(services, svc, host, vps, monkeypatch)
    pair.lost = {"link_go"}
    old_gw_pub, installed = vps.gw_pub, host.installed()
    rid = await _to_switch(pair)
    assert not pair.on_same_keys(), "модель: link_go потерян — линк обязан лечь"
    await pair.run_until(clock, 170)
    assert not await pair.session() and pair.server_notes == []
    await pair.run_until(clock, _vps.rekeymod.SWITCH_SECONDS + 60)
    assert pair.on_same_keys(), "ВПС не вернул прежние — линк мёртв"
    assert vps.peer_pub() == old_gw_pub and host.installed() == installed
    assert host.link_runs == [], f"шлюз переставлял линк: {host.link_applies}"
    assert len(pair.server_notes) == 1 and "вернул прежние" in pair.server_notes[0], pair.server_notes
    assert pair.to_gateway.count("link_cancel") == 1, pair.to_gateway
    assert host.txn() == {} and host.etc_files() == set(), "согласие на шлюзе не снято отзывом"
    assert pair.c.notes == ["Запрос отозван сервером AWG"], f"агенту ушло: {pair.c.notes}"
    assert services.gateway_rekey(1) == {} and vps.extra_files() == set()
    assert services.gateway_rekey_cancel_take(1) == "", "отзыв остался в очереди после доставки"
    assert "link" not in svc.cfg_questions() and rid


async def test_new_keys_that_never_come_up_bring_both_sides_back(services, slot, vps, svc, host, clock, monkeypatch):
    """link_go дошёл, обе стороны переключились, но новый линк не поднялся
    (сеть рвётся): шлюз за 90 с вернулся на прежние, ВПС за 180 с — тоже;
    поиск находит их без перебора, первое слово сервера закрепляет выбор
    (link_done keys old); по одному уведомлению с каждой стороны."""
    pair = _Pair(services, svc, host, vps, monkeypatch)
    old_gw_pub = vps.gw_pub
    await _to_switch(pair)
    host.broken = True
    clock.at(10)
    await pair.tick()                                     # переподключиться не на чем — сеть рвётся
    assert services.gateway_rekey(1)["state"] == "switched"
    clock.at(gwrekey.SWITCH_SECONDS + 5)
    await pair.tick()
    assert host.txn()["state"] == "back" and pair.c.notes == [BACK_NOTE]
    clock.at(_vps.rekeymod.SWITCH_SECONDS + 5)
    await pair.tick()
    host.broken = False
    assert pair.on_same_keys() and vps.peer_pub() == old_gw_pub
    clock.at(_vps.rekeymod.SWITCH_SECONDS + 15)
    await pair.tick()                                     # сеть вернулась — клиент переподключился
    done = [b for k, b in pair.c.sent if k == "link_done"]
    assert done and done[-1]["keys"] == "old" and done[-1]["ok"] is False, pair.c.sent
    runs = len(host.link_runs)
    await pair.run_until(clock, clock.shift + 3600, step=60)
    assert len(host.link_runs) == runs, "живой линк переставили поиском"
    assert len(pair.server_notes) == 1 and len(pair.c.notes) == 1, (pair.server_notes, pair.c.notes)
    assert host.txn() == {} and services.gateway_rekey(1) == {}


async def test_a_gateway_that_cannot_raise_the_new_keys_brings_the_vps_back_at_once(
        services, slot, vps, svc, host, clock, monkeypatch):
    """Скрипт шлюза отказал новому конфигу на link_go: шлюз ответил declined,
    ВПС не остаётся на новых ключах до срока; обе стороны на прежних сразу,
    админу — одно уведомление с причиной, человеку — «вернул прежние»."""
    pair = _Pair(services, svc, host, vps, monkeypatch)
    old_gw_pub, installed = vps.gw_pub, host.installed()
    host.link_rc = 1
    await _to_ready(pair)
    await asyncio.sleep(0.05)
    assert vps.peer_pub() == old_gw_pub and host.installed() == installed and pair.on_same_keys()
    assert len(pair.server_notes) == 1 and "не прошёл проверку" in pair.server_notes[0], pair.server_notes
    assert pair.c.notes == [BACK_NOTE], pair.c.notes
    assert vps.extra_files() == set() and services.gateway_rekey(1).get("state") == "declined"
    assert host.etc_files() == {"link.txn"} and host.txn()["state"] == "declined"


async def test_a_lost_server_answer_is_found_by_the_search(services, slot, vps, svc, host, clock, monkeypatch):
    """hello новым ключом дошёл (ВПС зафиксировал), а ответ сервера шлюзу
    потерялся: шлюз за 90 с вернулся на прежние — линк мёртв; через 3 минуты
    и второй замер поиск ставит новые, линк оживает, ответ сервера
    закрепляет новые ключи."""
    pair = _Pair(services, svc, host, vps, monkeypatch)
    await _to_switch(pair)
    # сессия на новых ключах открылась на ВПС, первое слово до шлюза не дошло
    pair.session_keys = pair._keys()
    pair.server._sessions[1] = types.SimpleNamespace(hello=True, caps=["cfg1", "confirm1"], rekey_sent="")
    assert await pair.server.rekey_hello(1) is True
    assert services.gateway_rekey(1) == {} and "ключи линка сменены" in pair.server_notes[-1]
    new_gw_pub = vps.peer_pub()
    clock.at(gwrekey.SWITCH_SECONDS + 5)
    await pair.tick()
    assert not pair.on_same_keys(), "модель: шлюз вернулся на прежние, ВПС на новых"
    await pair.run_until(clock, gwrekey.SWITCH_SECONDS + 5 + gwrekey.SEARCH_FIRST + 2 * gwrekey.DEAD_CONFIRM_SECONDS)
    assert pair.on_same_keys(), "поиск не нашёл новые ключи"
    assert vps.peer_pub() == new_gw_pub
    assert pair.c.notes[-1] == "✅ Линк на новых ключах"
    assert host.txn() == {} and host.etc_files() == {"issued_at"}


# ── контракт: настоящий скрипт стадии → проверка шлюза ───────────────────────

class _RealScript:
    """Настоящий routing-link-setup.sh под Services: `id` и `awg` — подставные
    (awg genkey/pubkey дают настоящие X25519 через nacl: шлюз проверяет ключи
    по-настоящему), каталоги ВПС — временные. Конфиг шлюза — тот, что сервер
    шлёт документом (gwcfg_link_conf), как он и стоял бы на шлюзе."""

    def __init__(self, services, tmp_path, monkeypatch, server_host: str = ""):
        import os
        import sys
        from awgbot.core import config, settings
        from awgbot.domain.services.gwchannel import cfg as cfgmod
        from tests.conftest import fake_cmd
        from tests.unit.test_gwlink_rekey import vps_conf
        self.awg = tmp_path / "vps-awg"; self.awg.mkdir()
        self.root = tmp_path / "vps-root"; self.root.mkdir()
        bin_dir = tmp_path / "vps-bin"; bin_dir.mkdir()
        py = sys.executable
        fake_cmd(bin_dir / "id", "echo 0\n")
        fake_cmd(bin_dir / "awg",
                 'case "$1" in\n'
                 f'  genkey|genpsk) "{py}" -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())" ;;\n'
                 f'  pubkey) read k; "{py}" -c "import sys,base64;from nacl.public import PrivateKey;'
                 'print(base64.b64encode(bytes(PrivateKey(base64.b64decode(sys.argv[1])).public_key)).decode())" "$k" ;;\n'
                 'esac\n')
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}")
        monkeypatch.setenv("CONF_DIR", str(self.awg))
        monkeypatch.setenv("GW_CONF_OUT", str(self.root / "gw-awglink.conf"))
        monkeypatch.setenv("_APP_YAML", str(tmp_path / "нет.yaml"))
        monkeypatch.setenv("CLIENT_SUBNET", "10.8.1.0/24")
        monkeypatch.setattr(config, "AWG_DIR", str(self.awg))
        real_open = open

        def _open(path, *a, **kw):
            p = str(path)
            if p.startswith("/root/"):
                p = str(self.root / p[len("/root/"):])
            return real_open(p, *a, **kw)
        monkeypatch.setattr(_vps.rekeymod, "open", _open, raising=False)
        monkeypatch.setattr(cfgmod, "open", _open, raising=False)
        conf = {"app.network.server_host": server_host, "app.network.subnet_cidr": "10.8.1.0/24",
                "app.client_config.keepalive_seconds": "25-35"}
        real_get = settings.get
        monkeypatch.setattr(settings, "get", lambda k, d=None: conf[k] if k in conf else real_get(k, d))
        vpriv, vpub = keypair()
        gpriv, gpub = keypair()
        (self.awg / "awglink.conf").write_text(vps_conf(vpriv, gpub, "DUMMY-LINK-PSK="), encoding="utf-8")
        # источник конфига шлюзовой стороны — как его написал --apply (хост — тот, что был тогда)
        (self.root / "gw-awglink.conf").write_text(gw_conf(gpriv, vpub, "DUMMY-LINK-PSK="), encoding="utf-8")


def _gateway_stands_on_the_document(services, host) -> None:
    gw = services.db.gateway(1)
    text = services.gwcfg_link_conf(gw, services.gwcfg_env(gw))
    assert text, services.gwcfg_link_error(1)
    host.link.write_text(text, encoding="utf-8")


def test_keys_staged_by_the_real_script_pass_the_gateway_check(services, slot, svc, host, tmp_path, monkeypatch):
    """Сквозной контракт: настоящий скрипт стадии → тело link_next на ВПС →
    проверка шлюза. Шлюз принимает новые ключи и показывает тот же отпечаток,
    что карточка слота на ВПС."""
    _RealScript(services, tmp_path, monkeypatch)
    _gateway_stands_on_the_document(services, host)
    st = services.gateway_rekey_stage(1)
    body = services.gateway_rekey_next_body(1)
    res = svc.rekey_next_in({"t": "link_next", **body})
    assert res["reply"]["state"] == "asked", res
    assert svc.rekey_question()["fp"] == st["fp"], "отпечатки на ВПС и в чате агента разошлись"


def test_keys_staged_after_a_domain_change_pass_the_gateway_check(services, slot, svc, host, tmp_path, monkeypatch):
    """После «✏️ Домен» шлюз стоит на Endpoint с новым хостом — его довёз
    документ; `/root/gw-<линк>.conf` остался с прежним (его правит только
    --bundle). Новые ключи обязаны прийти с тем же Endpoint, что стоит, иначе
    шлюз откажет и смена ключей не пройдёт никогда."""
    _RealScript(services, tmp_path, monkeypatch, server_host="vps2.example.org")
    _gateway_stands_on_the_document(services, host)
    assert "Endpoint = vps2.example.org:51821" in host.installed()
    services.gateway_rekey_stage(1)
    res = svc.rekey_next_in({"t": "link_next", **services.gateway_rekey_next_body(1)})
    assert res["reply"]["state"] == "asked", f"шлюз отказал новым ключам: {res['reply']}"
