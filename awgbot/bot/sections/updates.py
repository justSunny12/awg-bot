"""«⬆️ Обновления»: проверка при открытии, «⬆️ Обновить до vX», тумблер
уведомлений (мьют в БД, не YAML), цикл расписания день → неделя → месяц.
Шаги установки, «В меню» на итоге и «Не уведомлять» — handlers/updates_flow;
их кнопки (UpdateCB) общие у ролей и возвращают на главную роли через
реестр экранов."""
from __future__ import annotations

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from awgbot.bot import texts
from awgbot.bot import ui
from awgbot.bot.callbacks import UpdateCB
from awgbot.bot.handlers import updates_flow
from awgbot.bot.handlers.common import call, cleanup_content, send_menu
from awgbot.core import settings

from ._kb import _chk, back_button

ID, LABEL, BACK = "upd", "⬆️ Обновления", "root"
KEYS = ("updates.poll_schedule",)
BOUNDS: dict = {}
UPDATE_SCHEDULE_CYCLE = texts.UPDATE_SCHEDULE_CYCLE
UPDATE_SCHEDULE_LABELS = texts.UPDATE_SCHEDULE_LABELS
CYCLES = {"updates.poll_schedule": UPDATE_SCHEDULE_CYCLE}
AFTER_CYCLE_KEY = "cached"      # после цикла — раздел без похода в сеть
ACTIONS: dict = {}


def keyboard(br, muted: bool, target_tag: str = "", blocked: str = "") -> InlineKeyboardMarkup:
    """target_tag — найденная цель (кнопка «⬆️ Обновить до vX», если не
    заблокирована); «Уведомлять» — мьют в БД; «Проверка» — цикл."""
    sched = str(settings.get("updates.poll_schedule", "day")).lower()
    if sched not in UPDATE_SCHEDULE_LABELS:
        sched = "month"
    tag = target_tag if str(target_tag).startswith("v") else f"v{target_tag}"
    return ui.rows(
        (f"⬆️ Обновить до {tag}", UpdateCB(action="install")) if target_tag and not blocked else None,
        [(f"{_chk(not muted)} Уведомлять", br.cb.pack(ID, "toggle", "notify")),
         (f"📅 Проверка: {UPDATE_SCHEDULE_LABELS[sched]}", br.cb.pack(ID, "cycle", "updates.poll_schedule"))],
        back_button(br))


async def screen(br, services, key: str = ""):
    """key «cached» — тумблер и цикл: без похода в сеть, по тегу последней
    проверки; иначе — проверка при открытии. «никогда» из старого конфига —
    «месяц» и уведомления выкл."""
    await call(services.normalize_update_schedule)
    if key == "cached":
        tag = await call(services.update_available_tag)
        found = updates_flow.CachedTarget(tag) if tag else None
        blocked = ""
    else:
        found = await call(services.update_scan)
        blocked = await call(services.update_block_reason, found) if found is not None else ""
    text = texts.settings_upd_text(None, found, blocked,
                                   scan_failed=bool(getattr(services, "update_scan_failed", False)))
    return text, keyboard(br, await call(services.updates_muted),
                          target_tag=found.tag if found is not None else "", blocked=blocked)


async def open(ctx) -> bool:  # noqa: A001 — имя действия раздела
    """Раздел ходит к списку релизов — колбэк отвечаем сразу."""
    from . import render
    await ctx.cb.answer(ui.Toast.checking)
    await render(ctx.cb, ctx.br, ctx.services, ID)
    return True


async def toggle(ctx) -> bool:
    """Тумблер «Уведомлять» — мьют в БД, не ключ conf; раздел — из кэша."""
    if ctx.key != "notify":
        return False
    from . import render
    await updates_flow.toggle_mute(ctx.cb, ctx.services)
    await render(ctx.cb, ctx.br, ctx.services, ID, "cached")
    return True


async def _main_anew(message, services, keep_id=None) -> None:
    """Главная роли новым сообщением — выход из потока обновления."""
    from awgbot.bot import screens
    await cleanup_content(message.bot, services, message.chat.id)
    parts = await screens.render("main", services=services, role="admin", chat_id=message.chat.id)
    if parts is not None:
        await send_menu(message, services, *parts, keep_id=keep_id)


async def install(cb: CallbackQuery, services) -> None:
    await updates_flow.install(cb, services, return_panel=_main_anew)


async def menu(cb: CallbackQuery, services, state: FSMContext) -> None:
    async def _back(message, services_):
        await _main_anew(message, services_, keep_id=message.message_id)
    await updates_flow.menu(cb, services, state, return_panel=_back)


async def mute(cb: CallbackQuery, services) -> None:
    await updates_flow.mute(cb, services)


def register_update_buttons(router) -> None:
    """UpdateCB: установка, «В меню» на итоге, «Не уведомлять» — на роутере
    общих разделов, у обеих ролей."""
    router.callback_query(UpdateCB.filter(F.action == "install"))(install)
    router.callback_query(UpdateCB.filter(F.action == "menu"))(menu)
    router.callback_query(UpdateCB.filter(F.action == "mute"))(mute)
