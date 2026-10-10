"""panel.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from awgbot.bot import keyboards as kb
from awgbot.bot import texts, ui
from awgbot.bot.callbacks import GwCB
from awgbot.bot.filters import RoleFilter
from awgbot.bot.handlers.common import call, edit_nav, send_menu, send_menu_to, purge_menus
from awgbot.runtime import linkclient
from awgbot.bot.handlers import gateway as pkg       # имена пакета — через него: их подменяют тесты


log = logging.getLogger("awgbot.gateway")

router = Router(name="gateway")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))

# Бандл ~25 КБ; всё, что заметно крупнее, — не наш файл. Лимит спасает от
# скачивания случайно пересланного видео.
_BUNDLE_MAX_BYTES = 512 * 1024


def _snapshot_max_age() -> float:
    """Снимок тика годится для панели, пока не старше двух тиков: пропущенный
    тик — уже повод сходить живьём, а не показывать позавчерашнее."""
    from awgbot.core import settings
    return 2 * 60 * settings.get_int("app.gateway.monitor_minutes", 3)


async def _status(services, fresh: bool):
    """fresh — живой снимок (и он же сохраняется для следующих показов);
    иначе снимок последнего тика, если свежий, а нет — живой."""
    if not fresh:
        st = await call(services.cached_status, pkg._snapshot_max_age())
        if st is not None:
            return st
        return await call(services.snapshot)
    await call(services.invalidate_static)               # «Статус»: и статику живьём
    return await call(services.snapshot)


async def _panel(target, services, cb: CallbackQuery | None = None, fresh: bool = False,
                 keep_id=None):
    """Панель — через нав-хелперы: одно живое меню в чате, прошлое гаснет,
    история ведётся для /start."""
    st = await pkg._status(services, fresh)
    lan = bool(getattr(st, "lan", None))
    tag = await call(services.update_available_tag)
    pending = await call(services.cfg_questions)        # вопросы с сервера AWG ждут человека
    if cb is not None:
        await edit_nav(cb, services, texts.gateway_panel(st, tag, linkclient.view(), pending), kb.gateway_panel_kb(pending=bool(pending), lan=lan))
    else:
        await send_menu(target, services, texts.gateway_panel(st, tag, linkclient.view(), pending), kb.gateway_panel_kb(pending=bool(pending), lan=lan),
                        keep_id=keep_id)


async def _panel_parts(services, *, fresh: bool = False):
    """Текст и клавиатура панели агента — для показа без входящего сообщения."""
    st = await pkg._status(services, fresh=fresh)
    pending = await call(services.cfg_questions)
    return (texts.gateway_panel(st, await call(services.update_available_tag), linkclient.view(), pending),
            kb.gateway_panel_kb(bool(getattr(st, "lan", None)), pending=bool(pending)))


async def restore_panel_after_restart(bot, services) -> None:
    """Исполнить обещание «вернётся через несколько секунд» — как у основного:
    обещание подменяется отчётом и остаётся в чате, панель — следующим
    сообщением. Зовётся новым процессом на старте."""
    waiting = await call(services.pop_restart_wait)
    if waiting is None:
        return
    chat_id, mid = waiting
    try:
        await bot.edit_message_text(texts.BOT_RESTARTED, chat_id=chat_id,
                                    message_id=mid, reply_markup=None)
    except Exception:                                  # noqa: BLE001
        pass
    await send_menu_to(bot, services, chat_id, *await pkg._panel_parts(services))


_FIRST_PANEL_KEY = "gw_first_panel_sent"


async def send_first_panel(bot, services) -> None:
    """Первый запуск после установки: панель админу сама, если диалог уже
    есть — при опознании кодом админ боту писал, и установщик обещает «бот
    напишет сам». Из файла первого применения диалога нет: Telegram не даёт
    боту начать первым, отправка не проходит — молчим, панель придёт на
    /start, как и сказано в инструкции."""
    from awgbot.core import config
    if await call(services.db.get_state, pkg._FIRST_PANEL_KEY):
        return
    try:
        await send_menu_to(bot, services, config.ADMIN_ID, *await pkg._panel_parts(services))
    except Exception:                                  # noqa: BLE001
        return                                         # диалога ещё нет
    await call(services.db.set_state, pkg._FIRST_PANEL_KEY, "1")


@router.message(CommandStart())
async def gw_start(message: Message, services, state: FSMContext):
    await state.clear()
    await call(services.db.set_state, pkg._FIRST_PANEL_KEY, "1")   # диалог есть — первая панель не нужна
    await purge_menus(message.bot, services, message.chat.id)
    await pkg._panel(message, services)


@router.callback_query(GwCB.filter(F.action == "panel"))
async def gw_panel(cb: CallbackQuery, services, state: FSMContext):
    await cb.answer()
    await state.clear()
    await pkg._panel(cb.message, services, cb)


@router.callback_query(GwCB.filter(F.action == "refresh"))
async def gw_refresh(cb: CallbackQuery, services, state: FSMContext):
    """«Обновить» — единственная кнопка, которая всегда ходит по пробам живьём."""
    await state.clear()
    await cb.answer("Снимаю показания…")
    await pkg._panel(cb.message, services, cb, fresh=True)


@router.callback_query(GwCB.filter(F.action == "health"))
async def gw_health(cb: CallbackQuery, services):
    await cb.answer(ui.Toast.checking)
    await call(services.invalidate_static)               # монитор здоровья — всё живьём
    st = await call(services.snapshot)         # с сохранением: панель и здоровье из одного момента
    await edit_nav(cb, services, texts.gateway_health(st), kb.gateway_health_kb())

