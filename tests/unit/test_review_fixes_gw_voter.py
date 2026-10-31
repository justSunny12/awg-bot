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
from tests.unit.test_quorum_cov_gw_agent import NAME, _seeing, _tick

pytestmark = pytest.mark.unit
__all__ = ["clock", "gw", "host"]


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
