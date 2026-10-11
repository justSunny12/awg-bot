"""Старт агента шлюза и токен бота (runtime/main.py, run_gateway): отозванный
токен не роняет агента — он живёт без Telegram (канал до сервера AWG,
планировщик) и ждёт новый токен каналом; после перезапуска на токене с
сервера — одно сообщение «🤖 Бот шлюза на новом токене…» и вопросы из того же
документа, которые прежний бот показать не мог.

Цена ошибки: агент, упавший на отозванном токене, — шлюз без канала, и новый
токен довезти нечем: только визит к машине; поллинг на мёртвом токене — цикл
отказов в журнале; сообщение о смене, снятое до доставки, — человек так и не
узнает, что бот снова жив; показанное повторно — шум на каждом старте.

Сборка роли настоящая до конца run_gateway. Подменены: Telegram (двойник
aiogram.Bot из теста имени бота агента), диспетчер (поллинг записывается и
останавливает сборку), планировщик, клиент канала, уведомления и всё, что
на старте ходит в Telegram или на хост.
"""
from __future__ import annotations

import asyncio

import pytest
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.methods import GetMe

from awgbot.bot import notifier, texts
from awgbot.infra.db import Database
from awgbot.runtime import main
from tests.unit.test_gateway_agent_bot import _Agent

pytestmark = pytest.mark.unit


class _Stop(Exception):
    """Поллинг запущен — сборка дошла до конца."""


class _Start:
    """Весь старт агента до поллинга. notify_ok — дошло ли уведомление
    (notify_one); events — по порядку: ("notify", текст), "question",
    "channel", "polling"."""

    def __init__(self, monkeypatch, tmp_path):
        self.agent = _Agent(monkeypatch, tmp_path)
        self.tmp = tmp_path
        self.notify_ok = True
        self.events: list = []
        st = self

        async def notify_one(bot, chat_id, text, *a, **k):
            st.events.append(("notify", text))
            return st.notify_ok

        async def show_question(bot, services, chat_id):
            st.events.append("question")
            return True

        async def noop(*a, **k):
            return None

        class _Dp:
            def resolve_used_update_types(self):
                return []

            async def start_polling(self, *a, **k):
                st.events.append("polling")
                raise _Stop

        class _Watcher:
            def __init__(self, *a, **k):
                pass

            def start(self):
                pass

            def stop(self):
                pass

        class _Sched:
            def shutdown(self, wait=False):
                pass

        from awgbot.bot.handlers import gateway as gh
        from awgbot.bot.handlers import restore
        from awgbot.runtime import linkclient, poolprobe, preflight, scheduler
        monkeypatch.setattr(notifier, "set_email_fallback", lambda fn: None)
        monkeypatch.setattr(notifier, "notify_one", notify_one)
        monkeypatch.setattr(main, "ConfWatcher", _Watcher)
        monkeypatch.setattr(main, "make_dispatcher", lambda *a, **k: _Dp())
        monkeypatch.setattr(main, "_menu_command", noop)
        monkeypatch.setattr(main, "_announce_reboot", noop)
        monkeypatch.setattr(main, "report_update_result", noop)
        monkeypatch.setattr(preflight, "collect_warnings_gateway", lambda s: [])
        monkeypatch.setattr(poolprobe, "run", noop)
        monkeypatch.setattr(gh, "restore_panel_after_restart", noop)
        monkeypatch.setattr(gh, "send_first_panel", noop)
        monkeypatch.setattr(gh, "show_cfg_question", show_question)
        monkeypatch.setattr(restore, "report_restore_result", noop)
        monkeypatch.setattr(scheduler, "setup_gateway_scheduler", lambda s, b: _Sched())
        monkeypatch.setattr(linkclient, "ensure", lambda s: st.events.append("channel"))
        monkeypatch.setattr(linkclient, "shutdown", noop)

    def note_pending(self) -> None:
        """В БД агента — метка «перезапущен на токене с сервера» (как после
        применения раздела token)."""
        d = Database(str(self.tmp / "gw.db"))
        d.init_schema()
        d.set_state("cfg_token_note", "1")
        d.close()

    def pending_now(self) -> bool:
        """Метка в БД агента — свежим подключением (сборка закрывает своё)."""
        d = Database(str(self.tmp / "gw.db"))
        try:
            return bool(d.get_state("cfg_token_note"))
        finally:
            d.close()

    async def run_to_polling(self):
        with pytest.raises(_Stop):
            await main.run_gateway()

    @property
    def notes(self) -> list[str]:
        return [e[1] for e in self.events if isinstance(e, tuple) and e[0] == "notify"]


@pytest.fixture()
def start(monkeypatch, tmp_path):
    s = _Start(monkeypatch, tmp_path)
    return s


async def test_a_revoked_token_keeps_the_channel_and_skips_polling(start):
    """getMe ответил 401: канал до сервера поднят (новый токен приедет им),
    поллинга нет — агент ждёт; о смене токена не говорит даже при метке —
    говорить нечем."""
    start.agent.me = TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")
    start.note_pending()
    task = asyncio.ensure_future(main.run_gateway())
    done, _ = await asyncio.wait({task}, timeout=0.5)
    try:
        assert not done, f"агент с отозванным токеном завершился: {task.exception() if done else ''}"
        assert "channel" in start.events, "канал до сервера не поднят — новый токен довезти нечем"
        assert "polling" not in start.events, "поллинг на отозванном токене"
        assert texts.GW_TOKEN_SWITCHED not in start.notes
        assert start.pending_now(), "метка снята, хотя сказать было нечем"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_a_live_token_polls(start):
    await start.run_to_polling()
    assert start.events[-2:] == ["channel", "polling"], start.events


async def test_after_the_switch_the_new_bot_says_so_once_and_asks(start):
    """Первый старт на токене с сервера: одно сообщение GW_TOKEN_SWITCHED,
    метка снята, затем вопросы документа; следующий старт молчит."""
    start.note_pending()
    await start.run_to_polling()
    assert start.notes.count(texts.GW_TOKEN_SWITCHED) == 1, start.events
    i = start.events.index(("notify", texts.GW_TOKEN_SWITCHED))
    assert "question" in start.events[i + 1:], "вопросы документа не показаны новым ботом"
    assert not start.pending_now()
    start.events.clear()
    start.agent.getme_calls = 0
    await start.run_to_polling()
    assert texts.GW_TOKEN_SWITCHED not in start.notes, "сообщение о смене повторилось на следующем старте"


async def test_an_undelivered_switch_note_stays_for_the_next_start(start):
    """Сообщение не дошло (Telegram ещё недоступен) — метка остаётся,
    вопросы не показываются: скажем при следующем старте."""
    start.note_pending()
    start.notify_ok = False
    await start.run_to_polling()
    assert start.notes.count(texts.GW_TOKEN_SWITCHED) == 1
    assert "question" not in start.events
    assert start.pending_now(), "недоставленное сообщение сняло метку"


async def test_no_switch_note_without_the_mark(start):
    await start.run_to_polling()
    assert texts.GW_TOKEN_SWITCHED not in start.notes and "question" not in start.events
