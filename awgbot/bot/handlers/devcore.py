"""
handlers/devcore.py — шаги диалогов устройства, одинаковые у ролей: лимит
трафика, блокировка и разблокировка своей кнопкой, удаление держателем.
Роли различаются только поиском устройства (своё / удерживаемое / любое) и
карточкой, которую рисуют после — они приходят аргументами.
"""

from __future__ import annotations

from aiogram.types import CallbackQuery

from awgbot.bot import texts
from awgbot.bot.handlers.common import call, edit
from awgbot.bot.notifier import notify_one, send_notifications
from awgbot.core.blocks import DeviceBlock
from awgbot.domain.services import BYTES_PER_GB, ServiceError


async def apply_device_limit(services, dev, gb_value: int) -> tuple[str, bool]:
    """Лимит трафика устройства: не выше лимита профиля. (итог, применено)."""
    plimit = await call(services.profile_traffic_limit, dev.client_id)
    if plimit and gb_value * BYTES_PER_GB > plimit:
        return texts.device_limit_over(plimit), False
    old_b = int(dev.traffic_limit)
    new_b = gb_value * BYTES_PER_GB
    await call(services.set_device_traffic_limit, dev.id, new_b)
    return texts.limit_note(old_b, new_b, plimit), True


async def block_device(cb: CallbackQuery, services, dev, card) -> None:
    """Своя блокировка (бит USER); card(dev) → (текст, клавиатура) карточки."""
    notes = await call(services.block_device_manual, dev.id, DeviceBlock.USER, True)
    await send_notifications(cb.bot, notes)
    dev = await call(services.db.get_device, dev.id)
    await edit(cb, *await card(dev))
    await cb.answer("Заблокировано")


async def unblock_device(cb: CallbackQuery, services, dev, card) -> None:
    """Снимает ТОЛЬКО свой USER-бит; админские биты остаются."""
    if not (int(dev.block_reason) & int(DeviceBlock.USER)):
        await cb.answer("Ты не блокировал это устройство", show_alert=True)
        return
    notes = await call(services.unblock_device_manual, dev.id, DeviceBlock.USER, True)
    await send_notifications(cb.bot, notes)
    dev = await call(services.db.get_device, dev.id)
    await edit(cb, *await card(dev))
    await cb.answer("Разблокировано")


async def delete_by_holder(cb: CallbackQuery, services, dev) -> bool:
    """Держатель удалил переданное устройство сам: владельцу — уведомление
    с местами, итог на месте вопроса. False — отказ сервиса (ответ дан)."""
    try:
        await call(services.remove_device, dev.id)          # «удалено владельцем» ему не шлём
    except ServiceError as e:
        await cb.answer(str(e), show_alert=True)
        return False
    await cb.answer()
    if dev.owner_tg_id:
        used, limit = await call(services.device_quota, dev.client_id)
        await notify_one(cb.bot, dev.owner_tg_id,
                         texts.lent_device_deleted_by_holder_notice(dev, used, limit))
    await edit(cb, f"🗑 {texts._e(dev.name)} удалено", None)
    return True
