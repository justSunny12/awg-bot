"""E2E: отправка объявления — проверки до сброса черновика, «некому» без
второго живого меню, фоновое превью под присмотром; /start снимает пометку
«карточка с главной».

Стережём:
  • broadcast_send: отказ по длине / пустоте — черновик (FSM) цел, превью с
    кнопками живо, «сократи и пришли заново» действительно возможно;
  • «Некому отправлять»: итог — правкой текущего сообщения без кнопок, панель —
    следующим сообщением как единственное зарегистрированное меню;
  • отложенное превью альбома идёт через bgtask.spawn: исключение задачи
    попадает в журнал с трассой, а не теряется в «Task exception was never
    retrieved»;
  • admin_start: пометка «карточку открыли с главной» снимается — после
    /start «Назад» с карточки слота ведёт как обычно, не на главную.

Цена ошибки: state.clear() до проверок — админ видит «сократи и пришли
заново», а следующий текст уходит в никуда; меню на правке никто не
регистрирует — в чате два живых меню, инвариант «одно активное» держать
нечем; упавшее превью — тишина в чате и в журнале.
"""
import asyncio
import logging

import pytest

from awgbot.bot import bgtask
from awgbot.bot.handlers import common as _common
from awgbot.bot.handlers.admin import broadcast as admin_h
from awgbot.bot.handlers.admin import panel
from awgbot.core import config as cfg
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    admin_h._last_broadcast_at.clear()
    monkeypatch.setattr(_common, "_card_from_main", set())


def _cb(fake_bot):
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    return FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot), msg


async def test_a_refused_send_keeps_the_draft(services, make_active_client, fake_bot):
    c = make_active_client(name="Ксюша", tg_id=7301)
    state = FakeState()
    long_text = "я" * (cfg.TG_TEXT_MAX + 1)
    draft = {"targets": [c.id], "text": long_text, "text_len": len(long_text)}
    await state.update_data(**draft)
    cb, msg = _cb(fake_bot)
    await admin_h.broadcast_send(cb, state, services)
    assert cb.answers[-1][1] is True and "Не отправлено" in cb.answers[-1][0]
    assert await state.get_data() == draft, "черновик сброшен до проверки длины"
    assert not [r for r in fake_bot.records if r[0] == "send_message" and r[1] == 7301]
    assert not msg.sent, "превью переписано при отказе"


async def test_nobody_to_send_edits_in_place_and_registers_one_menu(services, make_active_client, fake_bot):
    c = make_active_client(name="Ксюша", tg_id=7302)
    services.db.update_client_fields(c.id, tg_id=None)      # профиль без Telegram: получателей нет
    state = FakeState()
    await state.update_data(targets=[c.id], text="Плановые работы")
    cb, msg = _cb(fake_bot)
    await admin_h.broadcast_send(cb, state, services)
    edits = [(t, mk) for kind, t, mk in msg.sent if kind == "edit_text"]
    assert edits and edits[-1][0].startswith("Некому отправлять") and edits[-1][1] is None, edits
    menus = [(t, mk) for kind, t, mk in msg.sent if kind == "answer" and mk is not None]
    assert len(menus) == 1, "панель не пришла следующим сообщением или пришла не одна"
    assert services.db.get_nav_message_id(cfg.ADMIN_ID) is not None, "панель не зарегистрирована как меню"
    assert await state.get_data() == {}


async def test_the_delayed_preview_runs_under_watch(services, make_active_client, fake_bot, monkeypatch, caplog):
    monkeypatch.setattr(admin_h, "_BC_SETTLE_SECONDS", 0)
    c = make_active_client(name="Ксюша", tg_id=7303)
    state = FakeState()
    await state.update_data(targets=[c.id])

    async def broken(message, state, services):
        raise RuntimeError("превью сломалось")
    monkeypatch.setattr(admin_h, "_bc_preview", broken)
    import types
    photo = [types.SimpleNamespace(file_id="F_small"), types.SimpleNamespace(file_id="F")]
    msg = FakeMessage(chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot, photo=photo)
    with caplog.at_level(logging.ERROR, logger="awgbot.bot.bgtask"):
        await admin_h.broadcast_receive(msg, state, services)
        task = admin_h._bc_render_tasks.get(cfg.ADMIN_ID)
        assert task is not None
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)                               # обработчик завершения
    hits = [r for r in caplog.records if "фоновая задача" in r.getMessage() and r.exc_info]
    assert hits and "превью сломалось" in hits[0].getMessage(), "исключение задачи не в журнале"


async def test_spawn_logs_the_exception_and_keeps_quiet_on_cancel(caplog):
    async def boom():
        raise ValueError("нет")

    async def forever():
        await asyncio.sleep(60)
    with caplog.at_level(logging.ERROR, logger="awgbot.bot.bgtask"):
        t = bgtask.spawn(boom(), name="boom")
        with pytest.raises(ValueError):
            await t
        c = bgtask.spawn(forever(), name="forever")
        await asyncio.sleep(0)
        c.cancel()
        with pytest.raises(asyncio.CancelledError):
            await c
        await asyncio.sleep(0)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("boom" in m and "ValueError" in m for m in msgs), msgs
    assert not any("forever" in m for m in msgs), "отмена попала в журнал как ошибка"


async def test_start_drops_the_card_from_main_mark(services, fake_bot):
    _common.card_from_main(cfg.ADMIN_ID, True)
    msg = FakeMessage(text="/start", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await panel.admin_start(msg, services, FakeState())
    assert not _common.card_is_from_main(cfg.ADMIN_ID), "/start не снял пометку «карточка с главной»"
