"""Итог аварийного кода — сразу в ответе (vote.reserve_code_in + codewait):
процесс открытия за секунды говорит, снялся ли ключ; «не подошёл» — попытка
в счёт, окно снова открыто, ответ на сообщение вместо уведомления; сбой
другого рода — окно открыто без попытки; не успел — «принят», итог придёт
уведомлением. Тексты Т-РН3 для каждого исхода.

Сценарий — `h4` из test_reserve_emergency_code (основа и её шлюзы молчат,
окно кода открыто); ход процесса — подменённый rsvzone.open_status.

Цена ошибки: «принят» при неподошедшем коде — админ основы ждёт переезда,
которого не будет, пока не придёт уведомление; ответ и уведомление разом —
два «не подошёл» на одну попытку; сбой зоны, засчитанный попыткой, — три
сбоя хоста резерва запирают ввод кода на час.
"""
from __future__ import annotations

import pytest

from awgbot.bot.texts.reserve import peeradmin as pa_t
from awgbot.core import config
from awgbot.infra import rsvzone
from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import PEER_ADMIN, box, notes
from tests.integration.test_reserve_emergency_code import CODE, h4

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["box", "h4"]

WRONG = "ключ пакета: SecretBox не снялся"
ZONE = "зона: veth не поднялся"


@pytest.fixture()
def run(h4, monkeypatch):
    """Ход процесса открытия: что скажет open.status к ответу."""
    h4.s.__dict__["_rsvopen_stop"] = lambda: None
    st = {"cur": {"step": "launching"}}
    monkeypatch.setattr(rsvzone, "open_status", lambda: dict(st["cur"]))
    h4.status = st
    return h4


def _tries(b) -> int:
    return len(b.s.rsv_get("rsv_view")["code"].get("tries") or [])


def test_a_code_that_took_the_key_is_accepted_without_pending(run):
    run.status["cur"] = {"step": "store"}
    res = run.s.reserve_code_in(PEER_ADMIN, CODE)
    assert res["ok"] and res["pending"] is False, res
    assert run.s.reserve_in() == "taking" and not run.s.reserve_code_window()["open"]


def test_no_answer_in_time_is_accepted_as_pending(run):
    res = run.s.reserve_code_in(PEER_ADMIN, CODE)
    assert res["ok"] and res["pending"] is True, res
    assert run.s.reserve_in() == "taking"


def test_a_wrong_code_is_answered_at_once_counted_and_the_window_reopened(run):
    run.status["cur"] = {"step": "fail", "reason": WRONG}
    res = run.s.reserve_code_in(PEER_ADMIN, CODE)
    assert (res["ok"], res["why"], res["checked"], res["left"]) == (False, "wrong", True, 2), res
    assert run.s.reserve_in() == "idle" and run.s.reserve_code_window()["open"], "окно не открыто снова"
    assert _tries(run) == 1, "неподошедший код не засчитан попыткой"
    told = notes(res["actions"])
    assert not [n for n in told if n.tg_id == PEER_ADMIN], "админу основы и ответ, и уведомление"
    assert [n for n in told if n.tg_id == config.ADMIN_ID], "админу резерва не сказано, что клиентов не принял"


def test_the_in_tick_after_an_answered_failure_says_nothing_more(run):
    run.status["cur"] = {"step": "fail", "reason": WRONG}
    run.s.reserve_code_in(PEER_ADMIN, CODE)
    assert not [n for n in notes(run.s.reserve_in_tick()) if n.tg_id == PEER_ADMIN], "второе «не подошёл» тактом"


def test_three_wrong_codes_lock_the_input_for_an_hour(run):
    run.status["cur"] = {"step": "fail", "reason": WRONG}
    lefts = [run.s.reserve_code_in(PEER_ADMIN, CODE).get("left") for _ in range(3)]
    assert lefts == [2, 1, 0], lefts
    assert run.s.reserve_code_in(PEER_ADMIN, CODE)["why"] == "limit"


def test_a_failure_of_another_kind_reopens_the_window_without_a_try(run):
    run.status["cur"] = {"step": "fail", "reason": ZONE}
    res = run.s.reserve_code_in(PEER_ADMIN, CODE)
    assert (res["ok"], res["why"], res["reason"], res["left"]) == (False, "open_failed", ZONE, 3), res
    assert run.s.reserve_code_window()["open"] and _tries(run) == 0, "сбой зоны засчитан попыткой кода"
    assert not [n for n in notes(res["actions"]) if n.tg_id == PEER_ADMIN]


def test_a_failure_without_a_reason_gets_a_default_one(run):
    run.status["cur"] = {"step": "fail"}
    res = run.s.reserve_code_in(PEER_ADMIN, CODE)
    assert res["why"] == "open_failed" and res["reason"], res


# ── тексты Т-РН3 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("res, want", [
    ({"ok": False, "why": "wrong", "checked": True, "left": 2}, "⚠️ Код не подошёл. Осталось попыток: 2 — следующие через час"),
    ({"ok": False, "why": "wrong", "checked": True, "left": 0}, "⚠️ Код не подошёл. Осталось попыток: 0"),
    ({"ok": False, "why": "wrong", "left": 1}, "⚠️ Код не разобран — проверь группы и знаки. Осталось попыток: 1 — следующие через час"),
    ({"ok": False, "why": "open_failed", "reason": ZONE},
     f"⚠️ Код подошёл, но пользователей «Beta» поднять не удалось: {ZONE}. Приём кода снова открыт"),
])
def test_the_answer_texts(res, want):
    assert pa_t.code_result(res, "Beta") == want


@pytest.mark.parametrize("pending, head, reservation", [(False, "✅ Код подошёл — поднимаю пользователей «Beta»", False),
                                                       (True, "✅ Код принят — поднимаю пользователей «Beta»", True)])
def test_the_accepted_answer_promises_a_follow_up_only_while_pending(pending, head, reservation):
    text = pa_t.code_result({"ok": True, "pending": pending}, "Beta")
    assert text.startswith(head) and ("если код не подойдёт" in text) is reservation, text


def test_the_answer_escapes_the_reason():
    text = pa_t.code_result({"ok": False, "why": "open_failed", "reason": "<b>x</b>"}, "Beta")
    assert "<b>x</b>" not in text and "&lt;b&gt;" in text


@pytest.mark.parametrize("res", [{"ok": True, "pending": True}, {"ok": True, "pending": False},
                                 {"ok": False, "why": "wrong", "checked": True, "left": 1},
                                 {"ok": False, "why": "open_failed", "reason": ZONE}])
def test_answers_have_no_final_period(res):
    assert not pa_t.code_result(res, "Beta").rstrip().endswith(".")
