"""«⚙️ Настройки» — корень: разделы роли по порядку словаря, по два в ряд,
«⬅️ В меню» — главная роли."""
from __future__ import annotations

from aiogram.utils.keyboard import InlineKeyboardBuilder

from awgbot.bot import texts

ID, LABEL, BACK = "root", "⚙️ Настройки", ""
KEYS: tuple[str, ...] = ()
BOUNDS: dict = {}
CYCLES: dict = {}
ACTIONS: dict = {}


def text(br) -> str:
    return texts.settings_root_text()


def keyboard(br):
    from . import label
    kb = InlineKeyboardBuilder()
    for sec in br.settings_root:
        kb.button(text=label(br, sec), callback_data=br.cb.pack(sec))
    kb.button(text="⬅️ В меню", callback_data=br.cb.menu())
    kb.adjust(2)
    return kb.as_markup()


async def screen(br, services, key: str = ""):
    return text(br), keyboard(br)
