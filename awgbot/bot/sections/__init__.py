"""sections/ — общие разделы настроек обеих ролей: по разделу на модуль, один
диспетчер на все действия, роль приходит словарём (bot/roles.py).

Модуль раздела объявляет всё сам: ID, LABEL, BACK (куда ведёт «⬅️ Назад»),
KEYS (ключи настроек раздела — по ним агент узнаёт раздел ключа), BOUNDS
(границы ввода), CYCLES (кнопки-циклы), ACTIONS (действия сверх общих
open/toggle/edit/cycle: функция `async fn(ctx) -> str | None` — строка идёт
первой строкой перерисованного раздела, None — действие нарисовало экран
само; или Confirm — подтверждение с ценой, исполняется по ключу с «!»),
text(br, …), keyboard(br, …), `async screen(br, services, key)`.

Отсюда выводится остальное: реестр SECTIONS из MODULES, доступность раздела
роли (ID в settings_root или subsections), границы и циклы по ключу, раздел
ключа, экран по имени (ролевые разделы — через br.role_screens), крючки для
ядра диалогов (settingscore) и фабрика роутера make_router(br) — один
обработчик на все колбэки общих разделов, с фильтром «переводчик роли узнал
колбэк и раздел доступен роли», без списка разделов. Ролевые разделы («🖥
Сервер AWG», «🛡 SSH-доступ», «💳 Подписки») остаются в обработчиках ролей.
"""
from __future__ import annotations

import importlib

from aiogram import Router
from aiogram.filters import Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from awgbot.bot import keyboards as kb
from awgbot.bot import ui
from awgbot.bot import texts
from awgbot.bot.filters import RoleFilter
from awgbot.bot.handlers.common import call, edit_nav
from awgbot.bot.screens import with_note
from awgbot.core import settings

from .base import Confirm, Ctx
from . import backup, email, mon, ncl, notify, root, svc, updates

MODULES = (root, notify, ncl, mon, email, backup, updates, svc)
SECTIONS = {m.ID: m for m in MODULES}
STALE = ui.Toast.stale


# ── реестр ───────────────────────────────────────────────────────────────────

def available(br, sec: str) -> bool:
    """Раздел есть у роли: общий модуль и ID в её корне или подразделах."""
    return sec in SECTIONS and (sec == "root" or sec in br.settings_root or sec in br.subsections)


def label(br, sec: str) -> str:
    """Подпись раздела для корня: модульный — LABEL, ролевой — из словаря роли."""
    if sec in SECTIONS:
        return SECTIONS[sec].LABEL
    return br.root_labels[sec]


def section_of(key: str) -> str:
    """Раздел ключа настройки по KEYS модулей; «» — ключ не общих разделов."""
    for m in MODULES:
        if key in m.KEYS:
            return m.ID
    return ""


def bounds(key: str):
    """(мин, макс, подпись, единица) ключа: сначала модули, потом таблица
    ролевых разделов (texts.SETTINGS_BOUNDS)."""
    for m in MODULES:
        if key in m.BOUNDS:
            return m.BOUNDS[key]
    return texts.SETTINGS_BOUNDS.get(key)


def default(key: str):
    """Умолчание ключа, пока его нет в conf (в единицах хранения): DEFAULTS
    модуля раздела; None — умолчания нет."""
    for m in MODULES:
        if key in getattr(m, "DEFAULTS", {}):
            return m.DEFAULTS[key]
    return None


def scale(key: str) -> int:
    """Множитель хранения ключа: человек вводит и видит одни единицы, conf
    хранит другие (ввод × множитель = значение в conf). SCALE модуля; 1 — нет."""
    for m in MODULES:
        if key in getattr(m, "SCALE", {}):
            return m.SCALE[key]
    return 1


def cycle_values(key: str):
    for m in MODULES:
        if key in m.CYCLES:
            return m.CYCLES[key]
    return None


def _role_screen_fn(br):
    mod, _, fn = br.role_screens.partition(":")
    return getattr(importlib.import_module(mod), fn)


async def screen(sec: str, br, services, key: str = ""):
    """(text, markup) раздела: модульный — его screen(br, …), ролевой — экраны роли."""
    if available(br, sec):
        return await SECTIONS[sec].screen(br, services, key)
    return await _role_screen_fn(br)(sec, services, key)


async def render(cb: CallbackQuery, br, services, sec: str, key: str = "", note: str = "") -> None:
    """Раздел на месте кнопки, живым меню; note — итог первой строкой."""
    text, markup = await screen(sec, br, services, key)
    await edit_nav(cb, services, with_note(text, note), markup)


def hooks_for(br):
    """Крючки ядра диалогов (settingscore.Hooks) для роли — из словаря."""
    from awgbot.bot.handlers import settingscore as core
    return core.Hooks(
        email_offer_kb=lambda sec: email.offer_kb(br, sec),
        render=lambda cb, services, sec: render(cb, br, services, sec),
        screen=lambda services, sec: screen(sec, br, services),
        br=br,
    )


# ── диспетчер ────────────────────────────────────────────────────────────────

def next_in_cycle(values, current):
    """Следующее значение ряда; значение вне ряда (из конфига руками) —
    ближайшее большее, за последним — первое."""
    values = list(values)
    try:
        cur = type(values[0])(current)
    except (ValueError, TypeError):
        return values[0]
    if cur in values:
        return values[(values.index(cur) + 1) % len(values)]
    if isinstance(cur, (int, float)):
        bigger = [v for v in values if v > cur]
        return bigger[0] if bigger else values[0]
    return values[0]


def resolve(br, data: str):
    """(раздел, действие, ключ, значение) общего раздела роли или None —
    колбэк не наш (чужой класс, ролевой раздел, недоступный раздел)."""
    packed = br.cb.parse(data)
    if packed is None:
        return None
    sec, act, key, val = packed
    if not sec:
        # ключевое действие (tgl/edit/cyc у агента): раздел — по ключу; ключ
        # неизвестен (кнопка прежних выпусков) — наш, ответ «устарела»
        sec = section_of(key)
        if not sec:
            return "", act, key, val
    if not available(br, sec):
        return None
    return sec, act, key, val


async def handle(cb: CallbackQuery, packed, services, state: FSMContext, br) -> None:
    """Одно действие общего раздела: open / toggle / edit / cycle / do."""
    from awgbot.bot.handlers import settingscore as core
    sec, act, key, val = packed
    if sec not in SECTIONS:
        await cb.answer(STALE, show_alert=True)
        return
    module = SECTIONS[sec]
    hooks = hooks_for(br)
    ctx = Ctx(cb, services, state, br, sec, key, val)
    if act == "open":
        if state is not None:
            await state.clear()
        opened = getattr(module, "open", None)
        if opened is not None and await opened(ctx):
            return
        await render(cb, br, services, sec, key)
        await cb.answer()
        return
    if act == "toggle":
        toggled = getattr(module, "toggle", None)
        if toggled is not None and await toggled(ctx):
            return
        await core.toggle_bool(cb, services, hooks, key, sec)
        return
    if act == "edit":
        await core.start_edit(cb, services, hooks, state, key, sec)
        return
    if act == "cycle":
        cycled = getattr(module, "cycle", None)
        if cycled is not None and await cycled(ctx):
            return
        values = cycle_values(key)
        if values is None:
            await cb.answer(STALE, show_alert=True)
            return
        new = next_in_cycle(values, settings.get(key, values[0]))
        try:
            await call(settings.set_value, key, new)
        except settings.SettingsWriteError as e:
            await cb.answer(ui.toast(e), show_alert=True)
            return
        await cb.answer(texts.cycle_toast(key, new))
        await render(cb, br, services, sec, getattr(module, "AFTER_CYCLE_KEY", ""))
        return
    if act == "do":
        action = module.ACTIONS.get(key)
        if action is None and key.endswith("!"):
            base = module.ACTIONS.get(key[:-1])
            if isinstance(base, Confirm):
                note = await base.run(ctx)
                if note is not None:
                    await render(cb, br, services, sec, note=note)
                return
        if isinstance(action, Confirm):
            await edit_nav(cb, services, await action.question(ctx),
                           kb.confirm(br.cb.pack(sec), action.label, br.cb.pack(sec, "do", f"{key}!"),
                                      danger=action.danger))
            await cb.answer()
            return
        if action is not None:
            note = await action(ctx)
            if note is not None:
                await render(cb, br, services, sec, note=note)
            return
    await cb.answer(STALE, show_alert=True)


class _SectionFilter(Filter):
    """Колбэк общего раздела роли: переводчик узнал, раздел доступен.
    Наследник Filter: простой вызываемый объект aiogram звал бы синхронно и
    принимал бы корутину за «истину»."""

    def __init__(self, br):
        self.br = br

    async def __call__(self, cb: CallbackQuery) -> bool:
        return resolve(self.br, cb.data) is not None


def make_router(br) -> Router:
    """Свой Router на каждый диспетчер: общие разделы роли, их действия и
    обработчики ввода (значение, парольная фраза, мастер почты) — ровно один
    раз на диспетчер; плюс обновления (UpdateCB) с возвратом на главную роли."""
    from awgbot.bot.handlers import settingscore as core
    router = Router(name="sections")
    router.message.filter(RoleFilter("admin"))
    router.callback_query.filter(RoleFilter("admin"))

    @router.callback_query(_SectionFilter(br))
    async def on_section(cb: CallbackQuery, services, state: FSMContext):
        await handle(cb, resolve(br, cb.data), services, state, br)

    core.register(router, hooks_for(br), default_sec="root")
    updates.register_update_buttons(router)
    router.on_section = on_section                    # для тестов
    return router


__all__ = ["MODULES", "SECTIONS", "Ctx", "Confirm", "available", "label", "section_of", "bounds",
           "cycle_values", "screen", "render", "hooks_for", "resolve", "handle", "make_router",
           "next_in_cycle"]
