"""
handlers/admin/gateway.py — пометка шлюза по пересланному сообщению агента.
"""

from __future__ import annotations

from awgbot.bot import texts
from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import Message

from awgbot.bot.handlers.common import call
from awgbot.domain.services import ServiceError

router = Router(name="admin.gateway")


# ── шлюз условной маршрутизации: пометка по пересланному сообщению ──────────
# Агент шлюза после применения конфигурации присылает подписанное сообщение;
# админ пересылает его сюда. Подпись — ключом линка (общий секрет сторон).

def has_gw_token(text) -> bool:
    from awgbot.util import gwsign
    return gwsign.find_token(text or "") is not None


@router.message(F.text.func(has_gw_token), StateFilter(None))
async def gateway_claim_message(message: Message, services):
    """Запасной путь: пересланный от агента токен. Основной — «Назначить шлюз»
    в настройках. Назначен другой шлюз — отказ, менять его через настройки."""
    try:
        res = await call(services.gateway_claim, message.text or "")
    except (ServiceError, ValueError) as e:
        await message.answer(f"🛰 Сообщение шлюза не принято: {texts._e(str(e))}")
        return
    if res["status"] == "already":
        await message.answer(texts.gateway_claim_already(res["device"]))
        return
    await message.answer(texts.gateway_claim_marked(res["device"]))
    from awgbot.bot.handlers.settings import send_gw_bundle
    await send_gw_bundle(message, services)              # без отдельного нажатия
