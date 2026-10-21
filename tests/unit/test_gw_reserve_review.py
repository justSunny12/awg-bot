"""Агент шлюза и резерв после ревью (domain/gateway/reserve/store.py): линк к
резерву, не поднявшийся при применении раздела, — раздел принят, повтор
тактом раз в минуту; уведомления админу шлюза с доставкой — в очереди до
подтверждения отправки, повтор раз в минуту, пока условие верно и не вышли
сутки; своё о ходе дел переживает новый документ.

Обвязка — `gw`/`host` из test_gw_reserve_agent (база, rsvlink и запись
DNS подменены).

Цена ошибки: отказ подъёма линка как отказ раздела — сервер AWG считает
раздел отвергнутым до смены отпечатка, шлюз без резерва; без повтора — линк
не встанет, пока не придёт новый документ; уведомление о переезде, ушедшее в
мёртвый аплинк, — админ шлюза не узнает, что клиенты у резерва; повтор
устаревшего (пересылка уже закрыта) — ложная тревога; очередь, стёртая
новым документом, — недоставленное пропало.
"""
from __future__ import annotations

import pytest

from awgbot.domain.gateway.reserve import store as store_mod
from awgbot.infra.gwguard import rsv as rsvlink
from awgbot.util import timeutil

from tests.unit.test_gw_reserve_agent import _checked, gw, host

pytestmark = pytest.mark.unit
__all__ = ["gw", "host"]


# ── линк к резерву: повтор ──────────────────────────────────────────────────

@pytest.fixture()
def failing_up(host, monkeypatch):
    state = {"fail": "имя резерва не резолвится"}

    def up(conf, admin_ips=None):
        if state["fail"]:
            raise rsvlink.RsvError(state["fail"])
        return host._up(conf, admin_ips)
    monkeypatch.setattr(rsvlink, "up", up)
    return state


def test_a_link_that_does_not_come_up_keeps_the_section_and_says_it_will_retry(gw, host, failing_up):
    host.link = False
    res = gw.reserve_apply(_checked(gw.conf.replace("Jc = 5", "Jc = 6")))
    sec = gw.reserve_section()
    assert sec.get("conf") and sec.get("link_pending") == "имя резерва не резолвится", "раздел отвергнут из-за подъёма"
    assert "повторю" in res["note"], res


def test_the_link_is_retried_once_a_minute_and_a_snapshot_goes_when_it_is_up(gw, host, failing_up):
    host.link = False
    gw.reserve_apply(_checked(gw.conf.replace("Jc = 5", "Jc = 6")))
    assert gw.reserve_link_retry_tick(now=1000.0) == []
    assert gw.reserve_link_retry_tick(now=1030.0) == [] and host.ups[-1:] != ["x"]
    failing_up["fail"] = ""
    ups = len(host.ups)
    assert gw.reserve_link_retry_tick(now=1030.0) == [], "повтор чаще раза в минуту"
    assert len(host.ups) == ups
    assert gw.reserve_link_retry_tick(now=1061.0) == [("push",)], "линк встал — серверу не ушёл снимок"
    assert "link_pending" not in gw.reserve_section()
    assert gw.reserve_link_retry_tick(now=2000.0) == [], "встал — повторять нечего"


def test_a_link_that_vanished_from_the_host_is_raised_again(gw, host):
    host.link = False
    assert gw.reserve_link_retry_tick(now=5000.0) == [("push",)] and host.link


# ── уведомления с доставкой ─────────────────────────────────────────────────

def _note(gw, **kw):
    cur = gw.reserve_section()
    n = gw.reserve_note(cur, "🛟 DUMMY: клиенты переехали на резерв", **kw)
    store_mod._write(cur, gw._RSV_STORE)
    return n


def test_a_note_stays_queued_until_it_is_sent(gw, host):
    n = _note(gw, sound=True)
    assert len(gw.reserve_section()["pending_notes"]) == 1 and n.force_sound
    n.on_sent()
    assert gw.reserve_section()["pending_notes"] == [], "доставленное не снято с очереди"


def test_an_undelivered_note_is_repeated_once_a_minute(gw, host):
    _note(gw)
    first = gw.reserve_notes_retry(now=100.0)
    assert [n.text for n in first[0][1]] == ["🛟 DUMMY: клиенты переехали на резерв"]
    assert gw.reserve_notes_retry(now=130.0) == [], "повтор чаще раза в минуту"
    again = gw.reserve_notes_retry(now=161.0)
    assert again and len(again[0][1]) == 1
    again[0][1][0].on_sent()
    assert gw.reserve_notes_retry(now=300.0) == []


def test_a_note_whose_condition_is_gone_is_dropped(gw, host):
    host.open = ["198.51.100.0/24"]
    _note(gw, cond={"open": True})
    host.open = []                                            # пересылка уже закрыта — переезд кончился
    assert gw.reserve_notes_retry(now=100.0) == []
    assert gw.reserve_section()["pending_notes"] == [], "устаревшее уведомление осталось в очереди"


def test_a_note_older_than_a_day_is_dropped(gw, host):
    _note(gw)
    cur = gw.reserve_section()
    cur["pending_notes"][0]["at"] = timeutil.to_iso(timeutil.shift(timeutil.now(), -store_mod.NOTE_TTL_S - 60))
    store_mod._write(cur, gw._RSV_STORE)
    assert gw.reserve_notes_retry(now=100.0) == [] and gw.reserve_section()["pending_notes"] == []


def test_the_queue_survives_a_new_document(gw, host):
    _note(gw)
    gw.reserve_apply(_checked(gw.conf, state="serving", move=9))
    assert len(gw.reserve_section().get("pending_notes") or []) == 1, "новый документ стёр недоставленное"


# ── решение о пересылке: факт записи не прочитался ──────────────────────────

def _notes(acts) -> list:
    return [n for a in acts or [] if a and a[0] == "notify" for n in a[1]]


@pytest.mark.parametrize("was_open", [False, True])
def test_an_unreadable_record_fact_changes_nothing(gw, host, monkeypatch, was_open):
    """Факт записи не прочитался и кэша нет: ни открыть (переезда, может, и
    не было), ни закрыть (клиенты, может, уже у резерва)."""
    host.open = ["198.51.100.0/24"] if was_open else []
    monkeypatch.setattr(gw, "reserve_record_fact", lambda *a, **k: None)
    acts = gw.reserve_state_in("serving", 3)
    assert not [a for a in acts if a and a[0] in ("open", "close")], acts


def test_clients_moved_is_told_once_per_move_on_any_opening(gw, host):
    """Т-РА3 — при открытии пересылки (по слову резерва или по такту) один раз
    на номер переезда: закрылась и открылась снова в том же переезде — молчим."""
    host.fact = True
    first = gw.reserve_state_in("serving", 3)
    host.run(first)
    assert host.open and len(_notes(first)) == 1, _notes(first)
    host.open = []                                            # пересылку закрыли (обрыв, перезапуск обвязки)
    again = gw.reserve_forward_decide()
    host.run(again)
    assert host.open and _notes(again) == [], "Т-РА3 второй раз в том же переезде"
    host.open = []
    gw.reserve_state_in("idle", 3)
    nxt = gw.reserve_state_in("serving", 4)
    host.run(nxt)
    assert len(_notes(nxt)) == 1, "новый переезд не объявлен"


# ── отпечаток раздела ───────────────────────────────────────────────────────

def test_the_section_print_covers_the_peer_host_record_name_and_slot(gw, host):
    """Отпечаток раздела в снимке — по нему сервер решает, принят ли раздел:
    смена адреса резерва, записи, имени или слота без смены отпечатка — шлюз
    живёт на прежних, а сервер считает раздел доставленным."""
    base = gw.reserve_print()
    assert base
    for field, value in (("peer_host", "198.51.100.99"), ("record", "vpn-z.example.org"), ("name", "Gamma"),
                         ("slot", 2)):
        gw.reserve_apply(_checked(gw.conf, **{field: value}))
        assert gw.reserve_print() != base, f"{field} не входит в отпечаток раздела"
        base = gw.reserve_print()
