"""Правки по ревью — голосующий кворума на шлюзе (domain/gateway/reserve/quorum.py и
соседи): первое слово новой сессии гостевого канала по номеру сессии, а не только по
флагу online раз в такт.

Хост и часы — как в tests/unit/test_gw_quorum.py (`_Host`, `_Clock`), quorum.json и
reserve.json — во временном каталоге, ответы лидера — настоящие датаграммы.

Цена ошибки: гостевой канал оборвался и поднялся между тактами — шлюз не замечает новой
сессии и молчит о своём взгляде; у резерва взгляды только в памяти, прежнее «нет» залипает
на живом канале голосом в счёте кандидата — ложное большинство и переезд при живой основе.
"""
from __future__ import annotations

import pytest

from tests.unit.test_gw_quorum import clock, gw, host
from tests.unit.test_gw_quorum_decision import ag
from tests.unit.test_quorum_cov_gw_agent import NAME, _seeing, _tick

pytestmark = pytest.mark.unit
__all__ = ["ag", "clock", "gw", "host"]


def _said(acts) -> list[dict]:
    return [a[2] for a in acts if a[0] == "guest" and a[1] == "qview"]


def _live(gw, clock) -> list[tuple]:
    """Такт, на каждую пробу которого лидер отвечает: взгляд «вижу» держится (без джиттерных пропусков)."""
    from awgbot.util.quorum.probe import Reply
    from tests.unit.test_quorum_cov_gw_agent import _probes
    acts = _tick(gw, clock)
    for p in _probes(acts):
        gw.quorum_reply_in(NAME, Reply("a", 4, 12, "lead", p.s, 2, 3).encode())
    return acts


def test_a_session_reopened_between_ticks_gets_the_current_view(gw, clock):
    _seeing(gw, clock)
    gw._quo_reserve_online, gw._quo_guest_session = True, 1
    first = _said(_live(gw, clock))
    assert len(first) == 1 and first[0]["w"] == "see", "подготовка: первая сессия без взгляда"
    assert not _said(_live(gw, clock)), "взгляд повторяется в той же сессии"
    gw._quo_guest_session = 2                                 # обрыв и подъём за один такт: online снова True
    again = _said(_live(gw, clock))
    assert len(again) == 1 and again[0]["w"] == "see" and again[0]["tgt"] == "h", \
        "новая сессия между тактами не замечена — резерв держит прежний взгляд шлюза"
    assert not _said(_live(gw, clock))


def test_a_session_number_seen_while_offline_is_said_once_online(gw, clock):
    _seeing(gw, clock)
    gw._quo_reserve_online, gw._quo_guest_session = False, 5
    assert not _said(_live(gw, clock)), "взгляд ушёл без сессии"
    gw._quo_reserve_online = True
    assert len(_said(_live(gw, clock))) == 1
    assert gw._gw_qvoters[NAME].view == "see"


# ── эпоха из ответа клона — всем голосующим линии ───────────────────────────

def test_the_epoch_of_the_clone_reply_goes_to_the_probe_of_the_home_server(gw, clock):
    """Пользователи основы у резерва: клон отвечает эпохой E (после взятия). Пробы к основе
    дома несут E — вернувшаяся основа следует, а не набирает «да» под прежней."""
    import json as _json

    from awgbot.util.quorum.probe import Probe
    from tests.unit.test_gw_quorum import _sec
    from tests.unit.test_quorum_cov_gw_agent import _answer, _probes
    gw.quorum_apply(_sec())
    gw.set_reserve(state="away")
    _answer(gw, clock, "awgqar", e=13)
    assert gw._gw_qvoters["awgqar"].epoch == 13, "подготовка: голосующий клона не принял эпоху ответа"
    assert gw._gw_qvoters[NAME].epoch == 13, "эпоха клона не дошла до голосующего о сервере дома"
    assert _json.loads(gw._QUO_STORE.read_text(encoding="utf-8")).get("e_known") == 13, \
        "эпоха из ответа клона не на диске — после перезапуска агента пробы с прежней"
    ps: list[Probe] = []
    for _ in range(10):
        ps += _probes(_tick(gw, clock), NAME)
        if ps:
            break
    assert ps and ps[-1].e == 13, f"проба к серверу дома с прежней эпохой: {[p.e for p in ps]}"


# ── qpause: длительность, а не срок по чужим часам ──────────────────────────

def test_qpause_with_a_duration_holds_even_when_the_clocks_differ(ag):
    """Часы лидера убежали на 10 минут назад: `until` по моим часам уже прошёл, `dur` — 120 с."""
    from awgbot.util import timeutil
    ag.see()
    past = timeutil.to_iso(timeutil.shift(timeutil.now(), -600))
    ag.word("qpause", until=past, dur=120)
    ag.run(100, answer=False, step=5)
    assert ag.v.view != "uns", "пауза лидера по расходящимся часам — ноль: «не вижу» во время обновления"
    ag.run(30, answer=False, step=5)
    assert ag.v.view == "uns", "после длительности паузы «не вижу» не наступило"


def test_qpause_duration_passes_the_guest_dictionary():
    from awgbot.util import pairproto as pp
    body = pp.check(pp.GUEST_PROTO, "qpause", {"l": "a", "until": "2026-10-31T10:00:00+03:00", "dur": 300}, pp.RESERVE)
    assert body.get("dur") == 300, "словарь гостевого канала отбросил длительность паузы"
    body = pp.check(pp.PAIR_PROTO, "qpause", {"l": "a", "until": "2026-10-31T10:00:00+03:00", "dur": 300}, pp.INVITER)
    assert body.get("dur") == 300, "словарь канала пары отбросил длительность паузы"
    assert "dur" not in pp.check(pp.GUEST_PROTO, "qpause", {"l": "a", "until": "2026-10-31T10:00:00+03:00"}, pp.RESERVE)


def test_pause_seconds_prefers_the_duration():
    from awgbot.util import timeutil
    from awgbot.util.quorum.voter import pause_seconds
    soon = timeutil.to_iso(timeutil.shift(timeutil.now(), 60))
    assert pause_seconds({"until": soon, "dur": 200}) == 200.0
    assert 55 <= pause_seconds({"until": soon}) <= 60, "без длительности — срок по своим часам, как прежде"
    assert pause_seconds({"until": "мусор", "dur": True}) == 0.0
