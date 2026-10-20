"""«⚙️ Настройки» — корень: разделы роли по порядку словаря, по два в ряд,
«⬅️ В меню» — главная роли."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from awgbot.bot import texts
from awgbot.bot import ui

ID, LABEL, BACK = "root", "⚙️ Настройки", ""
KEYS: tuple[str, ...] = ()
BOUNDS: dict = {}
CYCLES: dict = {}
ACTIONS: dict = {}


def text(br) -> str:
    return texts.settings_root_text()


def keyboard(br) -> InlineKeyboardMarkup:
    from . import label
    return ui.rows(*ui.grid([*[(label(br, sec), br.cb.pack(sec)) for sec in br.settings_root],
                             ui.to_menu(br.cb.menu())], 2))


async def screen(br, services, key: str = ""):
    if br.root_text:
        # роль со своим текстом корня («модуль:функция», async fn(services) -> str):
        # бот основы на резерве показывает здесь карточку резерва (Т-РГ3)
        import importlib
        mod, _, fn = br.root_text.partition(":")
        return await getattr(importlib.import_module(mod), fn)(services), keyboard(br)
    return text(br), keyboard(br)
