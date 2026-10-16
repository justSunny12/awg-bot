"""panel.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
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
    wait = await _update_wait(services, tag)
    pending = await call(services.cfg_questions)        # вопросы с сервера AWG ждут человека
    text = texts.gateway_panel(st, tag, linkclient.view(), pending, wait, _bot_username(services))
    markup = kb.gateway_panel_kb(pending=len(pending), lan=lan, update_tag=tag, broken=_has_problems(st))
    if cb is not None:
        await edit_nav(cb, services, text, markup)
    else:
        await send_menu(target, services, text, markup, keep_id=keep_id)


def _has_problems(st) -> bool:
    """Есть ли на панели что чинить: те же 🔴, что в «🩺 Здоровье», и сбой SMART."""
    from awgbot.domain import gwchecks
    smart = getattr(st, "smart", "") or ""
    return bool(gwchecks.health_problems(getattr(st, "checks", []) or [])) or bool(smart and smart != "OK")


def _bot_username(services) -> str:
    """Имя бота для ссылок «/start …» в тексте панели; до get_me — пусто, текст без ссылки."""
    return getattr(services, "bot_username", "") or ""


async def _update_wait(services, tag: str):
    """Ожидание сервера AWG — строкой панели, только когда «⬆️ Доступна» нет."""
    if tag or not hasattr(services, "update_waiting"):
        return None
    return await call(services.update_waiting)


async def _panel_parts(services, *, fresh: bool = False):
    """Текст и клавиатура панели агента — для показа без входящего сообщения."""
    st = await pkg._status(services, fresh=fresh)
    pending = await call(services.cfg_questions)
    tag = await call(services.update_available_tag)
    return (texts.gateway_panel(st, tag, linkclient.view(), pending, await _update_wait(services, tag),
                                _bot_username(services)),
            kb.gateway_panel_kb(bool(getattr(st, "lan", None)), pending=len(pending), update_tag=tag,
                              broken=_has_problems(st)))


async def panel_main_parts(services, chat_id: int):
    """Панель агента — главный экран роли (BotRole.main_screen)."""
    return await _panel_parts(services)


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
async def gw_start(message: Message, services, state: FSMContext, command: CommandObject | None = None):
    await state.clear()
    await call(services.db.set_state, pkg._FIRST_PANEL_KEY, "1")   # диалог есть — первая панель не нужна
    payload = ((command.args if command is not None else "") or "").strip()
    if payload == texts.REFRESH_PAYLOAD:
        await pkg._refresh_by_link(message, services)
        return
    await purge_menus(message.bot, services, message.chat.id)
    await pkg._panel(message, services)


async def _refresh_by_link(message: Message, services) -> None:
    """«🔄 обновлено …» в тексте панели — ссылка /start refresh: Telegram шлёт
    команду новым сообщением, бот её сразу убирает (как у экрана трафика
    основного бота) и перечитывает панель живьём на месте живого меню."""
    try:
        await message.delete()
    except Exception:                                  # noqa: BLE001
        pass
    st = await pkg._status(services, fresh=True)
    tag = await call(services.update_available_tag)
    pending = await call(services.cfg_questions)
    text = texts.gateway_panel(st, tag, linkclient.view(), pending, await _update_wait(services, tag),
                               _bot_username(services))
    markup = kb.gateway_panel_kb(bool(getattr(st, "lan", None)), pending=len(pending), update_tag=tag,
                              broken=_has_problems(st))
    nav_id = await call(services.db.get_nav_message_id, message.chat.id)
    if nav_id is not None:
        try:
            await message.bot.edit_message_text(text, chat_id=message.chat.id, message_id=nav_id,
                                                reply_markup=markup)
            return
        except Exception:                              # noqa: BLE001
            pass                                       # меню старое или уже такое — новым
    await send_menu(message, services, text, markup)


@router.callback_query(GwCB.filter(F.action == "panel"))
async def gw_panel(cb: CallbackQuery, services, state: FSMContext):
    await cb.answer()
    await state.clear()
    await pkg._panel(cb.message, services, cb)


@router.callback_query(GwCB.filter(F.action == "refresh"))
async def gw_refresh(cb: CallbackQuery, services, state: FSMContext):
    """Прежняя кнопка «🔄 Обновить» из старых меню в чатах: панель живьём."""
    await state.clear()
    await cb.answer("Снимаю показания…")
    await pkg._panel(cb.message, services, cb, fresh=True)


@router.callback_query(GwCB.filter(F.action == "health"))
async def gw_health(cb: CallbackQuery, services):
    await cb.answer(ui.Toast.checking)
    await call(services.invalidate_static)               # монитор здоровья — всё живьём
    st = await call(services.snapshot)         # с сохранением: панель и здоровье из одного момента
    await edit_nav(cb, services, texts.gateway_health(st), kb.gateway_health_kb())

