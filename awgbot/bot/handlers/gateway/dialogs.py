"""dialogs.py — часть пакета gateway (см. __init__)."""
from __future__ import annotations

from aiogram import Router
from awgbot.bot.roles import GATEWAY
from awgbot.bot.handlers import settingscore as core


_core = core.register(Router(name="gateway.dialogs"), __import__("awgbot.bot.sections", fromlist=["hooks_for"]).hooks_for(GATEWAY),
                      default_sec="mon")
gw_receive_value = _core["receive_value"]


def _hooks():
    from awgbot.bot import sections as secs
    return secs.hooks_for(GATEWAY)

