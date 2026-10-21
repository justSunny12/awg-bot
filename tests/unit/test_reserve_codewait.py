"""Итог аварийного кода ответом на сообщение (domain/services/reserve/codewait):
ждать первый шаг процесса открытия за конвертом ключа — «fail» — отказ, любой
шаг дальше launching/launched — ключ собран, срок вышел — «пока не ясно».

Ход процесса — подменённый rsvzone.open_status (последовательность шагов),
часы и сон — модельные (тест не спит).

Цена ошибки: «принят» при отказе — админ основы ждёт переезда, которого нет;
«launching» прежнего открытия, принятый за итог, — ответ «принят» до того,
как процесс вообще посмотрел на код; ожидание без срока — обработчик
сообщения висит, а с ним и чат роли.
"""
from __future__ import annotations

import types

import pytest

from awgbot.domain.services.reserve import codewait
from awgbot.infra import rsvzone

pytestmark = pytest.mark.unit


class _Proc:
    """Ход процесса открытия по опросам и модельные часы: каждый сон — шаг вперёд."""

    def __init__(self, monkeypatch, steps: list):
        self.steps = list(steps)
        self.t = 100.0
        self.polls = 0
        monkeypatch.setattr(rsvzone, "open_status", self.status)
        monkeypatch.setattr(codewait, "time", types.SimpleNamespace(monotonic=lambda: self.t, sleep=self.sleep))

    def status(self) -> dict:
        self.polls += 1
        cur = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        return cur if isinstance(cur, dict) else ({"step": cur} if cur else {})

    def sleep(self, s: float) -> None:
        self.t += s


@pytest.mark.parametrize("step", ["store", "k", "m", "parts", "clone_up", "done"])
def test_any_step_past_the_start_means_the_key_was_taken(monkeypatch, step):
    _Proc(monkeypatch, ["launching", "launched", step])
    assert codewait.verdict(5) == "ok"


def test_a_failure_is_reported_as_fail(monkeypatch):
    _Proc(monkeypatch, ["launched", {"step": "fail", "reason": "ключ пакета: не снялся"}])
    assert codewait.verdict(5) == "fail"


@pytest.mark.parametrize("steps", [["launching"], ["launched"], [""]])
def test_no_progress_within_the_term_is_unknown(monkeypatch, steps):
    p = _Proc(monkeypatch, steps)
    assert codewait.verdict(2) == ""
    assert p.t - 100.0 >= 2 and p.t - 100.0 <= 2 + codewait.POLL_S, "срок не выдержан или превышен"
    assert p.polls >= 2, "за срок ход процесса спрошен один раз"


def test_a_zero_term_looks_once_and_does_not_wait(monkeypatch):
    p = _Proc(monkeypatch, ["launching"])
    assert codewait.verdict(0) == "" and p.polls == 1 and p.t == 100.0


def test_a_zero_term_still_sees_a_ready_answer(monkeypatch):
    _Proc(monkeypatch, ["fail"])
    assert codewait.verdict(0) == "fail"


def test_the_default_term_is_verdict_s(monkeypatch):
    p = _Proc(monkeypatch, ["launching"])
    monkeypatch.setattr(codewait, "VERDICT_S", 1.0)
    assert codewait.verdict() == "" and 1.0 <= p.t - 100.0 <= 1.0 + codewait.POLL_S


def test_the_term_fits_a_chat_answer():
    """Ответ на сообщение — секунды: дольше — человек решит, что бот завис."""
    assert 0 < codewait.VERDICT_S <= 10 and 0 < codewait.POLL_S < codewait.VERDICT_S
