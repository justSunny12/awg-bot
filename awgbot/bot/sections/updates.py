"""«⬆️ Обновления» основного бота — раздел набора: сервер AWG и шлюзы на
одном выпуске. Проверка при открытии, «⬆️ Обновить до vX» (диалог и
ход — handlers/fleet), «🔁 Повторить» (набор откатился, догон не прошёл
трижды), тумблер уведомлений (мьют в БД), цикл расписания день → неделя →
месяц. «В меню» на итоге и «Не уведомлять» — handlers/updates_flow.

У агента шлюза раздела нет: шлюзы обновляются только по команде сервера
AWG; прежние кнопки агента — «Кнопка устарела…» (register_update_buttons).
"""
from __future__ import annotations

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from awgbot.bot import texts
from awgbot.bot import ui
from awgbot.bot.callbacks import GwCB, UpdateCB
from awgbot.bot.handlers import fleet as fleet_flow
from awgbot.bot.handlers import updates_flow
from awgbot.bot.handlers.common import call, cleanup_content, send_menu
from awgbot.bot.texts import fleet as ft
from awgbot.core import settings

from ._kb import _chk, back_button

ID, LABEL, BACK = "upd", "⬆️ Обновления", "root"
KEYS = ("updates.poll_schedule",)
BOUNDS: dict = {}
UPDATE_SCHEDULE_CYCLE = texts.UPDATE_SCHEDULE_CYCLE
UPDATE_SCHEDULE_LABELS = texts.UPDATE_SCHEDULE_LABELS
CYCLES = {"updates.poll_schedule": UPDATE_SCHEDULE_CYCLE}
AFTER_CYCLE_KEY = "cached"      # после цикла — раздел без похода в сеть
AGENT_STALE = "Кнопка устарела — обновления теперь в боте сервера AWG"


def keyboard(br, muted: bool, target_tag: str = "", blocked: str = "", retry: bool = False,
             running: bool = False) -> InlineKeyboardMarkup:
    """target_tag — цель набора (кнопка «⬆️ Обновить до vX», если не идёт ход
    и не заблокирована); retry — «🔁 Повторить» (набор откатился, откат не
    прошёл, догон не прошёл трижды); «Уведомлять» — мьют в БД; «Проверка» — цикл."""
    sched = str(settings.get("updates.poll_schedule", "day")).lower()
    if sched not in UPDATE_SCHEDULE_LABELS:
        sched = "month"
    tag = target_tag if str(target_tag).startswith("v") else f"v{target_tag}"
    return ui.rows(
        (f"⬆️ Обновить до {tag}", UpdateCB(action="install", tag=str(target_tag)))
        if target_tag and not blocked and not running and not retry else None,
        ("🔁 Повторить", br.cb.pack(ID, "do", "retry")) if retry and not running else None,
        [(f"{_chk(not muted)} Уведомлять", br.cb.pack(ID, "toggle", "notify")),
         (f"📅 Проверка: {UPDATE_SCHEDULE_LABELS[sched]}", br.cb.pack(ID, "cycle", "updates.poll_schedule"))],
        back_button(br))


def _retry_due(plan: dict) -> bool:
    if (plan.get("run") or {}).get("state") in ("rolled_back", "rollback_failed"):
        return True
    return any((g.get("upd_state") or {}).get("final") for g in plan.get("gateways") or [])


async def screen(br, services, key: str = ""):
    """key «cached» — тумблер и цикл: без похода в сеть; иначе — проверка при
    открытии (сохраняет и список выпусков для плана)."""
    await call(services.normalize_update_schedule)
    failed = False
    if key != "cached":
        _found, failed = await call(services.update_scan_checked)
    target = None if await call(services.fleet_running) else await call(services.fleet_target)
    blocked = await call(services.update_block_reason, target) if target is not None else ""
    plan = await call(services.fleet_plan, target)
    skipped = await call(services.update_skipped)
    # причина и тег — в HTML экрана: экранировать (ui.toast не экранирует), строка —
    # в пределе длины вместе со списком изменений
    tail = (f"⛔ Обновление до {texts._e(target.tag)} сейчас недоступно: {texts._e(ui.toast(blocked, 1000))}"
            if blocked else "")
    text = ft.fleet_section(plan, scan_failed=failed, skipped=skipped,
                            bot_username=getattr(services, "bot_username", "") or "", tail=tail)
    running = (plan.get("run") or {}).get("state") in ("running", "rolling_back")
    return text, keyboard(br, await call(services.updates_muted), target_tag=target.tag if target else "",
                          blocked=blocked, retry=_retry_due(plan), running=running)


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


async def _retry(ctx):
    """«🔁 Повторить» в разделе: ход заново (живое сообщение) или догоны заново."""
    await fleet_flow.retry(ctx.cb, ctx.services)
    if not await call(ctx.services.fleet_running):
        from . import render
        await render(ctx.cb, ctx.br, ctx.services, ID, "cached")
    return None


ACTIONS: dict = {"retry": _retry}


async def _main_anew(message, services, keep_id=None) -> None:
    """Главная роли новым сообщением — выход из потока обновления."""
    from awgbot.bot import screens
    await cleanup_content(message.bot, services, message.chat.id)
    parts = await screens.render("main", services=services, role="admin", chat_id=message.chat.id)
    if parts is not None:
        await send_menu(message, services, *parts, keep_id=keep_id)


async def menu(cb: CallbackQuery, services, state: FSMContext) -> None:
    async def _back(message, services_):
        await _main_anew(message, services_, keep_id=message.message_id)
    await updates_flow.menu(cb, services, state, return_panel=_back)


async def mute(cb: CallbackQuery, services) -> None:
    await updates_flow.mute(cb, services)


def register_update_buttons(router, br=None) -> None:
    """UpdateCB — на роутере общих разделов. Основной бот: диалог, ход,
    «🔁 Повторить», «В меню» на итоге, «Не уведомлять». Агент (роль без
    has.fleet): «В меню» на итоге — живая, прочие кнопки прежних «⬆️
    Обновлений» — «Кнопка устарела…»."""
    if br is not None and not br.has.fleet:
        if not br.has.updates_stale:
            return                                        # роль без обновлений вовсе: кнопки ловит её загородка
        # «⬅️ В меню» на итоге обновления (руками, awg-bot update) у агента живая
        router.callback_query(UpdateCB.filter(F.action == "menu"))(menu)

        async def _agent_stale(cb: CallbackQuery, services) -> None:
            await cb.answer(AGENT_STALE, show_alert=True)
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:                             # noqa: BLE001
                pass
        router.callback_query(UpdateCB.filter())(_agent_stale)
        router.callback_query(GwCB.filter(F.action.in_({"updates", "upd_toggle"})))(_agent_stale)
        return

    async def _rerender(cb, services):
        from . import render
        await render(cb, br, services, ID, "cached")

    async def _install(cb: CallbackQuery, callback_data: UpdateCB, services) -> None:
        await fleet_flow.install(cb, services, callback_data.tag,
                                 rerender=(lambda: _rerender(cb, services)) if br is not None else None)

    async def _go(cb: CallbackQuery, callback_data: UpdateCB, services) -> None:
        await fleet_flow.start(cb, services, callback_data.tag)

    async def _cancel(cb: CallbackQuery, services) -> None:
        await fleet_flow.cancel(cb, services, rerender=(lambda: _rerender(cb, services)) if br is not None else None)

    async def _retry_btn(cb: CallbackQuery, services) -> None:
        await fleet_flow.retry(cb, services)

    router.callback_query(UpdateCB.filter(F.action == "install"))(_install)
    router.callback_query(UpdateCB.filter(F.action == "go"))(_go)
    router.callback_query(UpdateCB.filter(F.action == "cancel"))(_cancel)
    router.callback_query(UpdateCB.filter(F.action == "retry"))(_retry_btn)
    router.callback_query(UpdateCB.filter(F.action == "menu"))(menu)
    router.callback_query(UpdateCB.filter(F.action == "mute"))(mute)
