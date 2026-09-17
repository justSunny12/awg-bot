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


async def test_offers_to_prepare_when_the_second_interface_is_missing(gen, sent):
    """Поколение выросло, а рычага нет: второй интерфейс не поднят (установщик
    его не трогал, пока шёл другой переезд). Молчать здесь — тупик: интерфейс
    заводит только обновление, а оно уже прошло. Предлагаем поднять кнопкой."""
    awglock.write_state(applied=1)
    await rt._notify_migration_needed(None, _Svc(available=False))
    assert len(sent) == 1
    _tg, text, markup = sent[0]
    assert "ядро нового поколения" in text.lower()
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Подготовить переезд" in l for l in labels)


# ── свой DNS-резолвер: инфобокс при старте до решения ─────────────────────────

class _DnsSvc:
    def __init__(self, due: bool, target="10.8.1.1"):
        self._due, self._target = due, target

    def private_dns_offer_due(self):
        return self._due

    def private_dns_info(self):
        return {"target": self._target, "mode": "public", "decision": ""}


async def test_private_dns_offer_comes_with_three_decisions(sent):
    await rt._notify_private_dns_offer(None, _DnsSvc(True))
    assert len(sent) == 1
    tg_id, text, markup = sent[0]
    assert tg_id == config.ADMIN_ID
    assert "10.8.1.1" in text and "через раз" in text and "переезд" in text.lower()
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("сейчас" in l for l in labels) and any("следующем переезде" in l for l in labels) \
        and any("Не нужно" in l for l in labels)


async def test_private_dns_offer_is_silent_once_decided(sent):
    await rt._notify_private_dns_offer(None, _DnsSvc(False))
    assert sent == []


async def test_bot_identity_is_synced_once_per_fingerprint(db, monkeypatch):
    """Имя/описания бота выставляются по отпечатку (имя, описания, версия) в
    state: совпал — ни одного запроса к Bot API на старте."""
    from types import SimpleNamespace
    calls = []

    class Bot:
        async def get_my_name(self): calls.append("get_name"); return SimpleNamespace(name="old")
        async def set_my_name(self, n): calls.append(("set_name", n))
        async def get_my_description(self): calls.append("get_desc"); return SimpleNamespace(description="d")
        async def set_my_description(self, d): calls.append(("set_desc", d))
        async def get_my_short_description(self): calls.append("get_short"); return SimpleNamespace(short_description="s")
        async def set_my_short_description(self, d): calls.append(("set_short", d))
        async def delete_my_commands(self): calls.append("del_cmds")

    monkeypatch.setattr(config, "BOT_NAME", "Very Oblivious")
    monkeypatch.setattr(config, "BOT_DESCRIPTION", "d")
    monkeypatch.setattr(config, "BOT_SHORT_DESCRIPTION", "s")
    assert await rt._sync_bot_identity(Bot(), db) is True
    assert calls == ["get_name", ("set_name", "Very Oblivious"), "get_desc", "get_short", "del_cmds"]
    calls.clear()
    assert await rt._sync_bot_identity(Bot(), db) is False
    assert calls == [], "отпечаток совпал, а к Bot API всё равно сходили"
    monkeypatch.setattr(config, "BOT_DESCRIPTION", "new")
    assert await rt._sync_bot_identity(Bot(), db) is True
    assert ("set_desc", "new") in calls
