"""Общие кусочки клавиатур разделов: «⬅️ Назад» по словарю роли, тумблер."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton

from awgbot.bot.keyboards.common import _chk, _tick


def back_button(br, sec: str = "root") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="⬅️ Назад", callback_data=br.cb.pack(sec).pack())


def button(text: str, cb) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb.pack())


__all__ = ["_chk", "_tick", "back_button", "button"]
