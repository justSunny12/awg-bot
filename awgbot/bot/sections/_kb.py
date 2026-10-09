"""Общие кусочки клавиатур разделов: «⬅️ Назад» по словарю роли, тумблер."""
from __future__ import annotations

from awgbot.bot import ui
from awgbot.bot.keyboards.common import _chk, _tick


def back_button(br, sec: str = "root") -> tuple:
    return ui.back(br.cb.pack(sec))


__all__ = ["_chk", "_tick", "back_button"]
