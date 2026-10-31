"""Разбор слов обычной сессии канала пары (runtime/pairlink/inbound.py
`_handle_safe`): тело, не прошедшее проверку словаря, рвёт сессию; упавший
обработчик — лог и следующее слово; пятый сбой одного вида за минуту — разрыв.

Цена ошибки: «ядовитое» слово (сбой обработчика на конкретном теле) рвало
сессию, вторая сторона слала его снова на следующей — вечный круг
переподключений, а с ним и все прочие слова пары стояли; без предела —
бесконечно падающий обработчик молча съедал бы поток слов.
"""
from __future__ import annotations

import pytest

from awgbot.runtime.pairlink import common as plc
from awgbot.runtime.pairlink.inbound import FAULTS_MAX, InboundMixin
from awgbot.util import pairproto

pytestmark = pytest.mark.unit


class _Side(InboundMixin):
    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    async def _handle(self, sess, msg):
        self.calls += 1
        raise self.exc


def _sess():
    return plc._Session(None, b"k" * 32, listener=True)


async def test_a_body_that_does_not_parse_breaks_the_session():
    side = _Side(pairproto.ProtocolError("st без поля ready"))
    assert await side._handle_safe(_sess(), {"t": "st"}) is False


async def test_a_failing_handler_skips_the_word_and_keeps_the_session():
    side = _Side(RuntimeError("база занята"))
    sess = _sess()
    assert await side._handle_safe(sess, {"t": "taken"}) is True
    assert await side._handle_safe(sess, {"t": "up"}) is True


async def test_the_same_word_failing_again_and_again_breaks_the_session():
    side = _Side(RuntimeError("база занята"))
    sess = _sess()
    results = [await side._handle_safe(sess, {"t": "taken"}) for _ in range(FAULTS_MAX)]
    assert results == [True] * (FAULTS_MAX - 1) + [False]
    assert await side._handle_safe(_sess(), {"t": "taken"}) is True, "счёт сбоев — на сессию"


async def test_faults_older_than_a_minute_do_not_count(monkeypatch):
    from awgbot.runtime.pairlink import inbound
    now = [1000.0]
    monkeypatch.setattr(inbound.time, "monotonic", lambda: now[0])
    side = _Side(RuntimeError("сбой"))
    sess = _sess()
    for _ in range(FAULTS_MAX - 1):
        assert await side._handle_safe(sess, {"t": "taken"}) is True
    now[0] += 61
    assert await side._handle_safe(sess, {"t": "taken"}) is True
