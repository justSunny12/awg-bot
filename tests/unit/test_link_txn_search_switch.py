"""
Смена ключей линка на шлюзе: переключение, отсчёт 90 с, поиск рабочего
конфига сутки и частый такт.
"""
from __future__ import annotations

import json

import pytest

from awgbot.domain import gwcfg
from awgbot.domain.gateway.cfg import rekey as gwrekey
from awgbot.util import wgconf
from tests.unit import _link_txn_common as _c
from tests.unit._link_txn_common import (
    BACK_NOTE, QUIET, _msg, _next_conf, _ready, _switched, _tries, gwcfg_render,
)

pytestmark = pytest.mark.unit

conf_dir, svc, clock, host = _c.conf_dir, _c.svc, _c.clock, _c.host


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
