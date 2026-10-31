"""Очередь «дорогих» слов канала пары (runtime/pairlink `send`,
`flush_outbox`): `down`, `taken`, `ask_no`, `evict`, `relay`, `qview`, не
ушедшие (сессии нет, обрыв), уходят в начале следующей сессии после `st`;
последнее значение вида выигрывает; слово старше OUTBOX_TTL_S не шлётся.

Цена ошибки: такт эти слова не повторяет — `taken`, потерянный на обрыве,
оставлял основу в «уезжаю» до таймаута переезда при боте, уже отвечающем на
резерве; `down` — основа ждала срыва, о котором ей не сказали; `qview` —
кандидат без голоса до следующей смены взгляда.
"""
from __future__ import annotations

import pytest

from awgbot.runtime import pairlink

pytestmark = pytest.mark.unit


class _Link:
    def __init__(self, ok: bool):
        self.ok = ok
        self.sent: list = []
        self.online = ok

    async def send(self, kind, body=None):
        if self.ok:
            self.sent.append((kind, body))
        return self.ok


async def test_a_dear_word_that_did_not_go_goes_with_the_next_session(monkeypatch):
    monkeypatch.setattr(pairlink, "_link", None)
    assert await pairlink.send("down", {"reason": "старое"}) is False
    assert await pairlink.send("down", {"reason": "зона не поднялась"}) is False
    assert await pairlink.send("ping", {"n": "0"}) is False             # не «дорогое» — не копится
    link = _Link(True)
    monkeypatch.setattr(pairlink, "_link", link)
    await pairlink.flush_outbox()
    assert link.sent == [("down", {"reason": "зона не поднялась"})], "ушло не последнее значение вида"
    await pairlink.flush_outbox()
    assert len(link.sent) == 1, "слово ушло дважды"


async def test_a_word_sent_later_replaces_the_queued_one(monkeypatch):
    monkeypatch.setattr(pairlink, "_link", _Link(False))
    await pairlink.send("qview", {"l": "a", "v": 1})
    link = _Link(True)
    monkeypatch.setattr(pairlink, "_link", link)
    await pairlink.send("qview", {"l": "a", "v": 2})
    await pairlink.flush_outbox()
    assert link.sent == [("qview", {"l": "a", "v": 2})], "устаревший взгляд ушёл после свежего"


async def test_an_old_word_is_dropped_and_a_failing_flush_keeps_its_age(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(pairlink.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(pairlink, "_link", _Link(False))
    await pairlink.send("taken", {})
    now[0] += pairlink.OUTBOX_TTL_S - 1
    await pairlink.flush_outbox()                                       # сессии снова нет
    now[0] += 2
    link = _Link(True)
    monkeypatch.setattr(pairlink, "_link", link)
    await pairlink.flush_outbox()
    assert link.sent == [] and not pairlink._outbox, "слово прежнего эпизода ушло новой сессии"
