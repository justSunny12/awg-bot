"""Состав линии кворума на стороне основы и второго сервера (quorum/members.py,
концепт «кворум» §2.2, §3.3, §3.7.10): предложение по фактам (пара, шлюзы с
`quo1`), `qmem` второму серверу с повтором не чаще 30 с, `qmem_ok` — состав
действует, поколение выше моего (основа восстановлена из копии) — новое
предложение поверх, Т-К14 в карточке, пока подтверждения нет; в отъезде и в
ходе переезда состав не меняется; второй сервер хранит состав чужой линии и
отвечает `qmem_ok`; слова второй стороне — только при живом канале пары.

Цена ошибки: состав, вступивший в силу без согласия второго сервера, — основа
сама убирает молчащий шлюз и набирает себе большинство; второй сервер,
поверивший составу без проверки, — голоса чужих шлюзов в счёте; состав,
сменившийся посреди переезда, — голоса считают по разным знаменателям, и
решение о пользователях принимают узлы, которых в момент аварии не было;
поколение восстановленной основы ниже подтверждённого — `qmem_ok` её
предложений не догоняют никогда, кворум не включится; `qmem` каждые 5 с —
лишний трафик канала пары и шум журнала.
"""
from __future__ import annotations

import pytest

from awgbot.bot.texts.reserve import section
from awgbot.runtime import pairlink, quorum as quorum_rt
from awgbot.runtime.scheduler import reserve as jobs
from tests.conftest import FakeBot
from tests.integration import _reserve_common as rc
from tests.integration._reserve_quorum import line
from tests.integration.test_reserve_jobs import _Sched

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["line"]


def _sent(actions, kind: str) -> list[dict]:
    return [a[2] for a in actions or [] if a and a[0] == "send" and a[1] == kind]


# ── предложение по фактам ───────────────────────────────────────────────────

def test_the_line_is_proposed_from_the_pair_and_the_gateways_that_announced_quo1(line):
    """Состав — оба сервера и свои шлюзы с `quo1`; шлюз без `quo1` (4.1.x) в
    ансамбль не входит: ни голоса, ни точки (§3.7.11)."""
    line.add_gw(1)
    line.add_gw(2, quo=False)
    line.add_gw(3)
    m = line.propose()
    assert m is not None and m.to_dict() == {"l": "a", "g": 1, "members": ["a", "b", "a1", "a3"]}, m
    assert line.s.reserve_quorum_pending(), "первый состав без qmem_ok уже считается действующим"
    assert line.s.reserve_quorum_mine() is None, "действующий состав до подтверждения второго сервера"


def test_the_same_facts_do_not_raise_the_generation(line):
    line.add_gw(1)
    first = line.propose()
    again = line.propose()
    assert (first.g, again.g) == (1, 1), "такт без смены фактов поднял поколение состава"


def test_without_a_pair_there_is_no_line_and_the_old_one_is_dropped(line):
    line.add_gw(1)
    line.propose()
    line.s.db.reserve_peer_delete()
    assert line.propose() is None
    assert not line.s.rsv_get("rsv_quorum"), "состав снятой пары остался — новая пара унаследует чужое поколение"


# ── qmem / qmem_ok ──────────────────────────────────────────────────────────

def test_qmem_goes_while_the_line_is_pending_and_not_more_often_than_every_30_seconds(line):
    line.add_gw(1)
    line.propose()
    body = line.s.reserve_qmem_body()
    assert body == {"l": "a", "g": 1, "members": ["a", "b", "a1"]}, body
    line.advance(29)
    assert line.s.reserve_qmem_body() is None, "qmem повторился раньше 30 с"
    line.advance(1)
    assert line.s.reserve_qmem_body() == body, "без ответа qmem не повторился через 30 с"
    line.ack_members()
    line.advance(60)
    assert line.s.reserve_qmem_body() is None, "после qmem_ok состав всё ещё шлётся"


def test_a_new_proposal_is_sent_at_once_even_inside_the_30_seconds(line):
    """Новое поколение — не повтор: ждать 30 с после прежнего qmem не нужно."""
    line.add_gw(1)
    line.propose()
    assert line.s.reserve_qmem_body()["g"] == 1
    line.add_gw(2)
    line.propose()
    body = line.s.reserve_qmem_body()
    assert body is not None and body["g"] == 2 and body["members"] == ["a", "b", "a1", "a2"], body


def test_a_new_pair_session_sends_the_pending_line_again_at_once(line):
    line.add_gw(1)
    line.propose()
    assert line.s.reserve_qmem_body() is not None
    assert line.s.reserve_qmem_body() is None
    line.s.reserve_session_opened({})
    assert line.s.reserve_qmem_body() is not None, "после новой сессии пары состав ждёт 30 с — второй сервер его не видел"


def test_no_qmem_to_a_peer_without_quo1(line):
    """«Beta» 4.1.x не знает вида qmem: незнакомый вид рвёт сессию канала пары (§3.3)."""
    line.peer_quo1(False)
    line.add_gw(1)
    line.propose()
    assert line.s.reserve_qmem_body() is None


def test_qmem_ok_of_the_proposed_generation_makes_the_line_active(line):
    line.add_gw(1)
    proposed = line.propose()
    assert line.s.reserve_qmem_ok_in({"l": "a", "g": 1}) == []
    assert line.s.reserve_quorum_mine() == proposed, "подтверждённый состав не стал действующим"
    assert not line.s.reserve_quorum_pending()
    assert "not_acked" not in line.s.reserve_quorum_off_reasons()


@pytest.mark.parametrize("body", [{"l": "a", "g": 0}, {"l": "b", "g": 1}, {"l": "", "g": 1}, {"g": 1}],
                         ids=["older_g", "other_line", "no_line", "missing_line"])
def test_qmem_ok_of_another_generation_or_line_changes_nothing(line, body):
    line.add_gw(1)
    line.propose()
    line.s.reserve_qmem_ok_in(body)
    assert line.s.reserve_quorum_mine() is None, f"{body}: подтверждение не моего предложения сделало состав действующим"
    assert line.s.reserve_quorum_pending()


def test_a_qmem_ok_above_my_generation_raises_the_proposal_over_it(line):
    """Основа восстановлена из копии: у «Beta» подтверждено поколение 5, у меня
    1. Новое предложение — 6 с тем же составом, и уходит сразу; само по себе
    чужое поколение состав действующим не делает."""
    line.add_gw(1)
    line.propose()
    assert line.s.reserve_qmem_body()["g"] == 1
    line.s.reserve_qmem_ok_in({"l": "a", "g": 5})
    m = line.s.reserve_quorum_proposed()
    assert m.g == 6 and m.members == ("a", "b", "a1"), m
    assert line.s.reserve_quorum_mine() is None, "поколение второй стороны сделало действующим не подтверждённый состав"
    body = line.s.reserve_qmem_body()
    assert body is not None and body["g"] == 6, "новое предложение ждёт повтора 30 с"
    line.s.reserve_qmem_ok_in({"l": "a", "g": 6})
    assert line.s.reserve_quorum_mine().g == 6 and not line.s.reserve_quorum_pending()


def test_while_the_peer_is_silent_the_previous_line_acts_and_the_card_says_so(line):
    """Шлюз 2 подключился и объявил quo1, «Beta» молчит: действует прежний
    состав — голосующие о «Alpha» те же «Beta» и шлюз 1, шлюз 2 не голосует;
    в карточке — Т-К14 «жду подтверждения «Beta»» (§3.7.10)."""
    line.add_gw(1)
    line.enable()
    line.add_gw(2)
    proposed = line.propose()
    assert proposed.g == 2 and "a2" in proposed.members
    mine = line.s.reserve_quorum_mine()
    assert mine.g == 1 and mine.voters("a") == ("b", "a1"), f"новый состав начал действовать без qmem_ok: {mine}"
    assert line.s.reserve_quorum_pending()
    card = line.s.reserve_quorum_card()
    assert card["pending"] and card["mine"]["g"] == 1 and card["mine"]["v"] == 2, card
    text = "\n".join(section.quorum_mine_lines(card))
    assert "Состав голосующих меняется: жду подтверждения «Beta»" in text, text
    line.ack_members()
    assert "Состав голосующих меняется" not in "\n".join(section.quorum_mine_lines(line.s.reserve_quorum_card())), \
        "Т-К14 осталась после подтверждения"


@pytest.mark.parametrize("state", ["away", "leaving", "returning"])
def test_the_line_does_not_change_while_the_users_are_away_or_moving(line, state):
    """Подключать шлюзы в отъезде нельзя и так; но агент шлюза 2 может
    обновиться до quo1 и в отъезде — состав всё равно ждёт возвращения."""
    line.add_gw(1)
    line.add_gw(2, quo=False)
    line.propose()
    line.s.reserve_set_out(state)
    line.gw_quo(2, True)
    m = line.propose()
    assert m.g == 1 and "a2" not in m.members, f"состав сменился в состоянии {state}"


@pytest.mark.parametrize("state", ["taking", "releasing"])
def test_the_line_does_not_change_while_the_peers_users_come_or_go(line, state):
    line.add_gw(1)
    line.add_gw(2, quo=False)
    line.propose()
    line.s.reserve_set_in(state)
    line.gw_quo(2, True)
    assert line.propose().g == 1, f"состав сменился в ходе {state}"


def test_the_change_held_back_by_the_move_is_proposed_once_the_users_are_back(line):
    line.add_gw(1)
    line.add_gw(2, quo=False)
    line.propose()
    line.s.reserve_set_out("away")
    line.gw_quo(2, True)
    line.propose()
    line.s.reserve_set_out("home")
    m = line.propose()
    assert m.g == 2 and m.members == ("a", "b", "a1", "a2"), m


def test_an_update_batch_of_the_gateways_makes_one_change_of_the_line(line):
    """Концепт «кворум» §5.1, §8.1, план quo-3: набор обновлений ведёт шлюзы по
    одному, каждый получает quo1 — смены копятся до конца хода и уходят одной
    (иначе поколение росло бы на каждый шлюз, а qmem и новые ключи шли бы
    второму серверу на каждом шаге набора)."""
    from awgbot.domain.fleet import state as fleet_state
    for slot in (1, 2, 3):
        line.add_gw(slot, quo=False)
    first = line.propose()
    assert first.g == 1 and not first.gateways
    line.s.fleet_run_set({"state": fleet_state.RUNNING})
    assert line.s.fleet_running()
    for slot in (1, 2, 3):
        line.gw_quo(slot, True)                          # шлюз обновлён набором, агент объявил quo1
        line.propose()
    line.s.fleet_run_set({})
    m = line.propose()
    assert m.members == ("a", "b", "a1", "a2", "a3"), m
    assert m.g == 2, f"набор из трёх шлюзов поднял поколение состава {m.g - 1} раз(а) — ждали одну смену"


# ── сторона второго сервера ─────────────────────────────────────────────────

def test_the_second_server_keeps_the_line_of_the_peer_and_answers_qmem_ok(line):
    """«Alpha» — сторона `a`; линия «Beta» — `b`: её состав ложится в peer и
    виден в карточке, ответ — qmem_ok того же поколения."""
    acts = line.s.reserve_qmem_in({"l": "b", "g": 3, "members": ["b", "a", "b1", "b2"]})
    assert acts == [("send", "qmem_ok", {"l": "b", "g": 3})], acts
    peer = line.s.reserve_quorum_peer_line()
    assert (peer["l"], peer["g"], peer["members"]) == ("b", 3, ["b", "a", "b1", "b2"]), peer
    assert line.s.reserve_quorum_proposed() is None, "состав второй линии лёг в мою"


@pytest.mark.parametrize("body", [
    {"l": "b", "g": 3, "members": ["b", "b1"]},
    {"l": "b", "g": 0, "members": ["b", "a", "b1"]},
    {"l": "b", "g": 3, "members": ["b", "a", "a1"]},
    {"l": "b", "g": 3, "members": ["b", "a", "b1", "b1"]},
    {"l": "b", "g": 3, "members": ["b", "a", "b99"]},
    {"l": "x", "g": 3, "members": ["x", "a"]},
    {"l": "b", "g": "три", "members": ["b", "a"]},
], ids=["one_server", "zero_g", "foreign_gateway", "twice", "big_slot", "bad_line", "g_not_number"])
def test_a_malformed_qmem_is_dropped_without_an_answer(line, body):
    try:
        acts = line.s.reserve_qmem_in(body)
    except (ValueError, TypeError) as e:
        pytest.fail(f"{body}: плохая форма уронила приём: {e!r}")
    assert acts == [], f"{body}: на плохую форму ушёл ответ {acts}"
    assert not line.s.rsv_get("rsv_quorum").get("peer"), f"{body}: плохой состав сохранён"


def test_a_qmem_of_my_own_line_is_not_taken_as_the_peers(line):
    """Слово с моей стороной линии — не состав второго сервера (отражение или подмена)."""
    assert line.s.reserve_qmem_in({"l": "a", "g": 7, "members": ["a", "b", "a1"]}) == []
    assert not line.s.rsv_get("rsv_quorum").get("peer")


def test_the_pair_channel_hands_qmem_and_qmem_ok_to_the_line(line):
    """Слова вида qmem/qmem_ok из канала пары доходят до состава (inbound)."""
    line.add_gw(1)
    line.propose()
    assert line.s.reserve_quorum_pair_in("qmem", {"l": "b", "g": 2, "members": ["b", "a"]}) == \
        [("send", "qmem_ok", {"l": "b", "g": 2})]
    line.s.reserve_quorum_pair_in("qmem_ok", {"l": "a", "g": 1})
    assert line.s.reserve_quorum_mine() is not None and line.s.reserve_quorum_mine().g == 1


# ── такт ────────────────────────────────────────────────────────────────────

def test_the_tick_sends_qmem_only_with_a_live_pair_session(line):
    line.add_gw(1)
    line.s.rsv_update("rsv_peer_state", session=False)
    assert not _sent(line.s.quorum_tick(), "qmem"), "qmem ушёл без сессии пары"
    line.s.rsv_update("rsv_peer_state", session=True)
    line.s.reserve_qmem_reset()
    got = _sent(line.s.quorum_tick(), "qmem")
    assert got == [{"l": "a", "g": 1, "members": ["a", "b", "a1"]}], got
    assert not _sent(line.s.quorum_tick(), "qmem"), "второй такт подряд повторил qmem"


async def test_the_scheduler_holds_back_words_to_the_peer_while_the_pair_channel_is_down(line, monkeypatch):
    """Такт кворума отдал qmem и уведомление, канала пары нет: уведомление
    уходит, слово второй стороне — нет (оно повторится тактом)."""
    ran: list = []

    async def run(acts):
        ran.extend(acts)
    from awgbot.runtime import rsvactions
    monkeypatch.setattr(rsvactions, "run", run)
    monkeypatch.setattr(quorum_rt, "_rt", None)
    monkeypatch.setattr(pairlink, "online", lambda: False)
    monkeypatch.setattr(pairlink, "send", lambda *a, **k: None)
    monkeypatch.setattr(line.s, "reserve_liveness_tick", lambda now=None: (None, []))
    monkeypatch.setattr(line.s, "reserve_vote_tick", lambda: [])
    monkeypatch.setattr(line.s, "reserve_st_if_changed", lambda: None)
    monkeypatch.setattr(line.s, "quorum_runtime_plan", lambda: {"responder": None, "probes": {}})
    monkeypatch.setattr(line.s, "quorum_tick", lambda: [("send", "qmem", {"l": "a", "g": 1}), ("notify", ["Т-К12"])])
    sched = _Sched()
    jobs.setup_reserve_jobs(sched, line.s, FakeBot())
    await sched.jobs["rsv_liveness"][0]()
    assert ("notify", ["Т-К12"]) in ran, f"уведомление такта кворума потерялось: {ran}"
    assert not [a for a in ran if a[0] == "send"], f"слово второй стороне ушло без канала пары: {ran}"
    ran.clear()
    monkeypatch.setattr(pairlink, "online", lambda: True)
    await sched.jobs["rsv_liveness"][0]()
    assert ("send", "qmem", {"l": "a", "g": 1}) in ran, f"при живом канале qmem не ушёл: {ran}"
