"""Голосующий кворума на шлюзе — то, чего нет в остальных тестах агента
(domain/gateway/reserve/quorum.py, vote.py, store.reserve_state_in): первое слово
новой гостевой сессии, эпоха из `state{e}` резерва, принятая эпоха после
перезапуска агента, мусор в словах кандидата и на диске, отказ снятия зонда,
забытый голосующий ушедшего зонда, разбор `q*` слов гостевым каналом.

Хост и часы — как в tests/unit/test_gw_quorum.py (`_Host`, `_Clock`, фикстуры
оттуда же): интерфейсы зондов — модель, quorum.json и reserve.json — во
временном каталоге, пробы и ответы — настоящие датаграммы.

Цена ошибки: после переподключения гостевого канала резерв не знает взгляда
шлюза (у него взгляды только в памяти) до следующей смены — «нет» шлюза не в
счёте, переезд при мёртвой основе ждёт лишние минуты; эпоха, принятая по
`qtake`, теряется при перезапуске агента — его пробы прежнему лидеру снова
«вижу» под старой эпохой, и тот возвращается к записи при живом новом держателе;
кривая отметка обязательства на диске роняет такт голосующего целиком.
"""
from __future__ import annotations

import json

import pytest

from awgbot.domain.gateway import GatewayServices
from awgbot.infra import quoprobe
from awgbot.infra.db import Database
from awgbot.util import timeutil
from awgbot.util.quorum.probe import Probe, Reply
from tests.unit.test_gw_quorum import _sec, _store, clock, gw, host

pytestmark = pytest.mark.unit
__all__ = ["clock", "gw", "host"]

NAME = "awgqah"


def _probes(actions, name: str = NAME) -> list[Probe]:
    return [Probe.parse(a[2]) for a in actions if a[0] == "probe" and a[1] == name]


def _tick(gw, clock, step: float = 5.0) -> list[tuple]:
    clock.t += step
    return gw.quorum_tick(clock.t)


def _answer(gw, clock, name: str = NAME, e: int = 12) -> list[tuple]:
    """Пора пробы — отвечает лидер; действия ответа."""
    out: list[tuple] = []
    for _ in range(10):
        acts = _tick(gw, clock)
        ps = _probes(acts, name)
        if ps:
            clock.t += 0.2
            return out + gw.quorum_reply_in(name, Reply("a", 4, e, "lead", ps[-1].s, 2, 3).encode())
    raise AssertionError("пробы нет за 50 с")


def _seeing(gw, clock) -> None:
    gw.quorum_apply(_sec())
    for _ in range(2):
        clock.t += 16
        _answer(gw, clock)
    assert gw._gw_qvoters[NAME].view == "see", "подготовка: шлюз не видит сервер"


def _restart(gw) -> GatewayServices:
    d = Database(gw._QUO_STORE.parent / "gw2.db")
    d.init_schema()
    svc = GatewayServices(d)
    svc._RSV_STORE, svc._QUO_STORE = gw._RSV_STORE, gw._QUO_STORE
    return svc


# ── первое слово новой гостевой сессии ──────────────────────────────────────

def test_a_new_guest_session_gets_the_current_view_about_the_server_once(gw, clock):
    """У резерва взгляды шлюзов — только в памяти: на подъём сессии гостевого канала
    шлюз первым словом говорит текущий взгляд о своём сервере, один раз."""
    _seeing(gw, clock)
    gw._quo_reserve_online = False
    _tick(gw, clock)
    gw._quo_reserve_online = True
    acts = _tick(gw, clock)
    first = [a for a in acts if a[0] == "guest" and a[1] == "qview"]
    assert len(first) == 1 and first[0][2]["w"] == "see" and first[0][2]["tgt"] == "h", acts
    assert first[0][2]["e"] == 12 and first[0][2]["l"] == "a"
    again = _tick(gw, clock) + _tick(gw, clock)
    assert not [a for a in again if a[0] == "guest"], "взгляд повторяется каждый такт открытой сессии"


def test_a_view_not_yet_formed_is_not_said_on_a_new_session(gw, clock):
    gw.quorum_apply(_sec())
    gw._quo_reserve_online = True
    acts = _tick(gw, clock)
    assert not [a for a in acts if a[0] == "guest"], "взгляд «ещё не смотрел» ушёл резерву за голос"


def test_the_view_about_the_clone_is_not_said_to_the_reserve_on_a_new_session(gw, clock):
    """Резерв цель зонда не различает: «не вижу клон» лёг бы у него взглядом на основу."""
    gw.quorum_apply(_sec())
    gw.set_reserve(state="away")
    for _ in range(2):
        clock.t += 16
        _answer(gw, clock, "awgqar")
    gw._quo_reserve_online = True
    acts = _tick(gw, clock)
    assert all(a[2].get("tgt") != "r" for a in acts if a[0] == "guest"), acts


def test_a_session_coming_back_says_an_uns_view_and_pledges_again(gw, clock):
    """Взгляд «не вижу» первым словом новой сессии — тоже «нет» кандидату: обязательство
    на диске под текущей эпохой."""
    gw.quorum_apply(_sec())
    for _ in range(12):
        _tick(gw, clock)
    assert gw._gw_qvoters[NAME].view == "uns", "подготовка: молчание не стало «не вижу»"
    cur = _store(gw)
    cur["pledges"] = {}
    gw._QUO_STORE.write_text(json.dumps(cur), encoding="utf-8")
    gw._quo_reserve_online = True
    acts = _tick(gw, clock)
    said = [a[2] for a in acts if a[0] == "guest" and a[1] == "qview"]
    assert said and said[0]["w"] == "uns", acts
    assert _store(gw)["pledges"].get(NAME, {}).get("e") == 12, "«нет» новой сессии без обязательства на диске"


# ── эпоха ───────────────────────────────────────────────────────────────────

def test_the_state_word_of_the_reserve_carries_the_epoch_to_the_probes(gw, clock, monkeypatch):
    gw.quorum_apply(_sec())
    _tick(gw, clock)
    monkeypatch.setattr(gw, "reserve_forward_decide", lambda: [])
    gw.reserve_guest_in("state", {"state": "serving", "move": 2, "e": 20})
    clock.t += 30
    ps = _probes(_tick(gw, clock))
    assert ps and ps[-1].e == 20, f"эпоха из state{{e}} резерва не дошла до проб: {ps}"


def test_a_state_word_without_an_epoch_leaves_the_known_one(gw, clock, monkeypatch):
    gw.quorum_apply(_sec())
    _tick(gw, clock)
    monkeypatch.setattr(gw, "reserve_forward_decide", lambda: [])
    gw.reserve_guest_in("state", {"state": "idle", "move": 2})
    assert gw._gw_qvoters[NAME].epoch == 12


def test_a_new_document_with_a_lower_epoch_does_not_lower_the_voter(gw, clock):
    gw.quorum_apply(_sec())
    _tick(gw, clock)
    gw.quorum_guest_in("qstate", {"l": "a", "e": 30})
    gw.quorum_apply(_sec(e=12, g=5))
    assert gw._gw_qvoters[NAME].epoch == 30, "документ со старой эпохой отмотал известную эпоху"


def test_qtake_with_an_older_epoch_keeps_the_known_one_and_lifts_the_pledge(gw, clock):
    gw.quorum_apply(_sec())
    _tick(gw, clock)
    v = gw._gw_qvoters[NAME]
    v.said_no(clock.t)
    gw._pledge_store(NAME, {"e": 12, "at": timeutil.now_iso()})
    gw.quorum_guest_in("qtake", {"l": "a", "e": 4})
    assert v.epoch == 12 and v.pledge is None and NAME not in _store(gw).get("pledges", {})


def test_the_epoch_taken_by_qtake_survives_an_agent_restart(gw, clock):
    """Концепт «кворум» §2.5: известная эпоха — в quorum.json. Кандидат сказал
    «забираю» под 13: после перезапуска агента пробы прежнему лидеру должны идти
    с 13, иначе он снова наберёт «да» под 12 при живом новом держателе (обязательство
    `qtake` уже снял — защищает только эпоха)."""
    _seeing(gw, clock)
    gw.quorum_guest_in("qtake", {"l": "a", "e": 12})
    assert gw._gw_qvoters[NAME].epoch == 13
    fresh = _restart(gw)
    clock.t += 5
    ps = _probes(fresh.quorum_tick(clock.t))
    assert ps and ps[0].e == 13, f"после перезапуска агента проба несёт эпоху {ps[0].e if ps else None}, а не 13"


def test_the_epoch_from_qstate_survives_an_agent_restart(gw, clock):
    gw.quorum_apply(_sec())
    _tick(gw, clock)
    gw.quorum_guest_in("qstate", {"l": "a", "e": 15})
    fresh = _restart(gw)
    clock.t += 5
    ps = _probes(fresh.quorum_tick(clock.t))
    assert ps and ps[0].e == 15, f"эпоха из qstate потеряна при перезапуске: {ps[0].e if ps else None}"


# ── мусор и отказы ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("until", ["", "вчера", "2026-13-45T99:00:00", None])
def test_qpause_with_a_broken_term_holds_nothing(gw, clock, until):
    gw.quorum_apply(_sec())
    _tick(gw, clock)
    assert gw.quorum_guest_in("qpause", {"l": "a", "until": until}) == []
    assert not gw._gw_qvoters[NAME].paused(clock.t + 1), "пауза без срока задержала «не вижу»"


def test_qpause_in_the_past_holds_nothing(gw, clock):
    gw.quorum_apply(_sec())
    _tick(gw, clock)
    past = timeutil.to_iso(timeutil.shift(timeutil.now(), -60))
    gw.quorum_guest_in("qpause", {"l": "a", "until": past})
    assert not gw._gw_qvoters[NAME].paused(clock.t + 1)


@pytest.mark.parametrize("at", ["", "garbage", None, 5])
def test_a_pledge_with_a_broken_mark_on_disk_is_dropped_at_restart(gw, clock, at):
    gw.quorum_apply(_sec())
    cur = _store(gw)
    cur["pledges"] = {NAME: {"e": 12, "at": at}}
    gw._QUO_STORE.write_text(json.dumps(cur), encoding="utf-8")
    acts = _tick(gw, clock)
    assert _probes(acts), "кривая отметка обязательства уронила такт"
    assert gw._gw_qvoters[NAME].pledge is None
    assert NAME not in _store(gw).get("pledges", {}), "обязательство с кривой отметкой осталось на диске"


def test_a_pledge_on_disk_with_a_later_epoch_raises_the_probes(gw, clock):
    gw.quorum_apply(_sec())
    cur = _store(gw)
    cur["pledges"] = {NAME: {"e": 14, "at": timeutil.now_iso()}}
    gw._QUO_STORE.write_text(json.dumps(cur), encoding="utf-8")
    ps = _probes(_tick(gw, clock))
    assert ps and ps[0].e == 14, "обязательство под эпохой 14 не подняло эпоху проб"


@pytest.mark.parametrize("kind,body", [
    ("qtake", {"l": "b", "e": 12}), ("qrel", {"l": "b", "e": 12}), ("qask", {"l": "b", "g": 4}),
    ("qneed", {"l": "b", "q_gen": 1}), ("qstate", {"l": "b", "e": 99}), ("qwhat", {"l": "a"}),
])
def test_words_of_another_line_or_unknown_kinds_change_nothing(gw, clock, kind, body):
    gw.quorum_apply(_sec())
    _tick(gw, clock)
    v = gw._gw_qvoters[NAME]
    before = (v.epoch, v.pledge, v.next_probe)
    assert gw.reserve_guest_in(kind, body) == []
    assert (v.epoch, v.pledge, v.next_probe) == before, f"{kind} {body} тронул голосующего"


def test_a_word_before_the_voter_exists_is_dropped(gw):
    gw.quorum_apply(_sec())
    for kind in ("qask", "qtake", "qrel", "qneed", "qpause"):
        assert gw.quorum_guest_in(kind, {"l": "a", "e": 12, "q_gen": 1, "until": ""}) == [], kind


def test_without_the_section_every_word_is_dropped(gw):
    assert gw.reserve_guest_in("qtake", {"l": "a", "e": 12}) == []
    assert not gw._QUO_STORE.exists(), "слово без раздела создало quorum.json"


def test_the_guest_channel_hands_quorum_words_to_the_voter(gw, clock):
    gw.quorum_apply(_sec())
    _tick(gw, clock)
    gw.reserve_guest_in("qtake", {"l": "a", "e": 20})
    assert gw._gw_qvoters[NAME].epoch == 21, "qtake гостевого канала не дошёл до голосующего"


def test_a_failing_probe_down_does_not_stop_the_wanted_probe(gw, host, monkeypatch):
    host.standing = {"awgqbh": "старый"}
    monkeypatch.setattr(quoprobe, "down", lambda name: (_ for _ in ()).throw(quoprobe.QuoError("занят")))
    gw.quorum_apply(_sec())
    assert ("awgqah", _sec()["probes"]["h"]) in host.ups, "отказ снятия лишнего зонда не дал поднять нужный"


def test_the_voter_of_a_probe_that_left_is_forgotten(gw, clock):
    """Пользователи вернулись — зонд к клону снят, его голосующий забыт: в следующем
    отъезде клон — другой хост, взгляд складывается заново."""
    gw.quorum_apply(_sec())
    gw.set_reserve(state="away")
    for _ in range(2):
        clock.t += 16
        _answer(gw, clock, "awgqar")
    assert gw._gw_qvoters["awgqar"].view == "see"
    gw.set_reserve(state="idle")
    _tick(gw, clock)
    assert "awgqar" not in gw._gw_qvoters, "голосующий снятого зонда остался"
    gw.set_reserve(state="away")
    _tick(gw, clock)
    assert gw._gw_qvoters["awgqar"].view == "new", "в новом отъезде взгляд на клон — от прошлого"


def test_the_reserve_state_word_without_a_section_does_nothing(tmp_path):
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    svc = GatewayServices(d)
    svc._RSV_STORE = tmp_path / "reserve.json"
    assert svc.reserve_state_in("serving", 3, e=40) == []
    assert not (tmp_path / "reserve.json").exists()


def test_a_new_document_keeps_the_known_epoch_mark_of_the_agent(gw, clock):
    """Свои отметки хода (`views`, `pledges`, `told`, `share_given_at`) новый документ не
    стирает — проверено в test_gw_quorum; здесь — что эпоха документа меньше
    отметки обязательства не опускает эпоху проб после перезапуска."""
    gw.quorum_apply(_sec())
    gw._pledge_store(NAME, {"e": 16, "at": timeutil.now_iso()})
    gw.quorum_apply(_sec(g=5))
    assert _store(gw)["pledges"][NAME]["e"] == 16
    fresh = _restart(gw)
    ps = _probes(fresh.quorum_tick(clock.t + 5))
    assert ps and ps[0].e == 16

