"""
Старт бота: сообщения, которые появляются сами. Здесь проверяется просьба о
переезде профилей — единственный способ узнать, что ядро сменило поколение.
Инверсия любого условия даёт либо молчание навсегда, либо просьбу при каждом
запуске.
"""
from __future__ import annotations

import pytest

from awgbot.core import config
from awgbot.infra import awglock
from awgbot.runtime import main as rt

pytestmark = pytest.mark.e2e


class _Svc:
    def __init__(self, *, running=False, available=True):
        self._running, self._available = running, available

    def migration_running(self):
        return self._running

    def migration_available(self):
        return self._available


@pytest.fixture()
def gen(tmp_path, monkeypatch):
    lock = tmp_path / "awg.lock"
    lock.write_text("AWG_GENERATION=2\nAWG_PROTOCOL_ID=amnezia-awg3\n", encoding="utf-8")
    monkeypatch.setattr(awglock, "LOCK_PATH", lock)
    monkeypatch.setattr(awglock, "STATE_PATH", tmp_path / "awg.state")
    return tmp_path


@pytest.fixture()
def sent(monkeypatch):
    out: list = []

    async def fake_notify(bot, tg_id, text, **kw):
        out.append((tg_id, text, kw.get("reply_markup")))
    monkeypatch.setattr(rt, "notify_one", fake_notify)
    return out


async def test_asks_for_migration_when_the_delivery_moved_ahead(gen, sent):
    awglock.write_state(applied=1)
    await rt._notify_migration_needed(None, _Svc())
    assert len(sent) == 1
    tg_id, text, markup = sent[0]
    assert tg_id == config.ADMIN_ID
    assert "переезд" in text.lower() and "поколения 2" in text
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Начать переезд" in l for l in labels), "просьба без кнопки — тупик"


async def test_silent_when_generations_match(gen, sent):
    awglock.write_state(applied=2)
    await rt._notify_migration_needed(None, _Svc())
    assert sent == []


async def test_silent_while_the_migration_is_already_running(gen, sent):
    awglock.write_state(applied=1)
    await rt._notify_migration_needed(None, _Svc(running=True))
    assert sent == [], "просьба переехать во время переезда"


async def test_warns_in_the_log_when_the_second_interface_is_missing(gen, sent, caplog):
    """Поколение выросло, а рычага нет: просить некуда, но и молчать нельзя —
    иначе хост остаётся на ядре, которое скоро перестанет обслуживать клиентов."""
    awglock.write_state(applied=1)
    with caplog.at_level("WARNING"):
        await rt._notify_migration_needed(None, _Svc(available=False))
    assert sent == []
    assert any("переезд" in r.message.lower() for r in caplog.records)
