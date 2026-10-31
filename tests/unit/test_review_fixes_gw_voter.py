"""Правки по ревью — голосующий кворума на шлюзе (domain/gateway/reserve/quorum.py и
соседи):

- первое слово новой сессии гостевого канала — по номеру сессии, а не только по флагу
  online раз в такт;
- эпоха из ответа клона — всем голосующим линии и на диск;
- `qpause` с длительностью `dur` — пауза не зависит от расхода часов;
- `qtake`/`qrel` до создания голосующего (первый такт после перезапуска) — на диск;
- восстановленное обязательство — не дольше pledge_s от старта при часах позади отметки;
  эпоха из слова или ответа — не дальше известной на EPOCH_STEP_MAX.

Хост и часы — как в tests/unit/test_gw_quorum.py (`_Host`, `_Clock`) и
tests/unit/test_gw_quorum_decision.py (`ag`), quorum.json и reserve.json — во временном
каталоге, ответы лидера — настоящие датаграммы.

Цена ошибки: гостевой канал оборвался и поднялся между тактами — шлюз молчит о взгляде,
у резерва прежнее «нет» залипает голосом в счёте кандидата — ложное большинство; пробы к
основе дома с эпохой до взятия — вернувшаяся основа набирает «да» при живом госте (два
писателя); пауза 0 по чужим часам — переезд во время обновления лидера; потерянный `qtake`
— пробы прежнему лидеру под старой эпохой; потерянный `qrel` — живой лидер без голоса до
срока; обязательство, продлённое часами, — голос потерян надолго; мусорная эпоха — линия
под эпохой, которой никто не держит.
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


# ── слово кандидата до создания голосующего (перезапуск агента) ──────────────

def _restart_before_tick(ag) -> None:
    ag.svc = ag.new()                                         # голосующего ещё нет: такта не было


def test_qtake_before_the_first_tick_is_kept_on_disk(ag):
    from tests.unit.test_gw_quorum_decision import E
    ag.see()
    ag.uns()
    _restart_before_tick(ag)
    assert NAME not in ag.svc._gw_qvoters
    ag.word("qtake", e=E)
    assert ag.section().get("e_known") == E + 1, "qtake до первого такта потерян — эпоха не поднята"
    assert NAME not in ag.pledges(), "qtake до первого такта не снял обязательство"
    ag.tick()
    assert ag.v.epoch == E + 1 and ag.v.pledge is None, "голосующий не взял с диска эпоху qtake"


def test_qrel_before_the_first_tick_lifts_the_pledge(ag):
    from tests.unit.test_gw_quorum_decision import E
    ag.see()
    ag.uns()
    _restart_before_tick(ag)
    ag.word("qrel", e=E + 1)
    assert NAME in ag.pledges(), "qrel чужой эпохи снял обязательство"
    ag.word("qrel", e=E)
    assert NAME not in ag.pledges(), "qrel до первого такта потерян — живой лидер без голоса до срока"
    ag.tick()
    assert ag.v.pledge is None


# ── обязательство и стенные часы; эпоха — недалеко от известной ─────────────

def test_a_pledge_stamped_in_the_future_lasts_no_longer_than_its_term(ag):
    """Часы без батарейки после перезагрузки — позади отметки: обязательство — не дольше
    pledge_s от старта агента, а не pledge_s плюс расход часов."""
    import json as _json

    from awgbot.util import timeutil
    from tests.unit.test_gw_quorum_decision import E
    sec = ag.section()
    sec["pledges"] = {NAME: {"e": E, "at": timeutil.to_iso(timeutil.shift(timeutil.now(), 3600))}}
    ag.store.write_text(_json.dumps(sec), encoding="utf-8")
    ag.svc = ag.new()
    ag.tick()
    assert ag.v.pledge is not None, "подготовка: обязательство не восстановлено"
    ag.run(ag.v.timing.pledge_s + 10, answer=True, step=5)  # лидер отвечает — нового «нет» нет
    assert ag.v.pledge is None and NAME not in ag.pledges(), "обязательство продлено расходом стенных часов"


def test_a_far_epoch_from_a_word_is_not_taken(ag):
    from tests.unit.test_gw_quorum_decision import E
    ag.word("qstate", e=E + 5000)
    assert ag.v.epoch == E and ag.section().get("e_known", 0) < E + 5000, "мусорная эпоха из qstate принята"
    ag.word("qstate", e=E + 3)
    assert ag.v.epoch == E + 3, "соседняя эпоха не принята"


def test_a_far_epoch_in_a_reply_keeps_the_view_but_not_the_epoch(ag):
    from tests.unit.test_gw_quorum_decision import E
    from awgbot.util.quorum.probe import Probe
    ag.run(60, answer=True, e=E + 5000)
    assert ag.v.view == "see", "ответ лидера с кривой эпохой не засчитан как жизнь"
    assert ag.v.epoch == E, "эпоха далеко впереди из ответа принята"
    ps = [Probe.parse(a[2]) for a in ag.run(30, answer=False) if a[0] == "probe"]
    assert ps and all(p.e == E for p in ps)
