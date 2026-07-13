"""E2E: admin-хендлеры ручной блокировки/разблокировки (callback-и BlockCB).

Проверяем маршрутизацию бита по kind (silent/notified), каскад на устройства,
проброс уведомлений через бота и ветку menu_unblock: одна причина → снимаем
сразу, несколько → диалог выбора.
"""
import pytest

from awgbot.bot.handlers import admin as admin_h
from awgbot.bot.callbacks import BlockCB
from awgbot.core import config
from awgbot.core.blocks import ClientBlock, DeviceBlock
from tests.conftest import FakeCallback, FakeMessage

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID


def _admin_cb(bot, data=""):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(data=data, message=nav, user_id=ADMIN, bot=bot), nav


# ── блок устройства ──────────────────────────────────────────────────────────
async def test_block_device_notified(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7100)
    dc = services.add_device(client.id, "d")
    cb, nav = _admin_cb(fake_bot)
    await admin_h.admin_block_do(
        cb, BlockCB(target="dev", action="block", ref=dc.device_id, kind="notified"), services)
    dev = services.db.get_device(dc.device_id)
    assert int(dev.block_reason) & int(DeviceBlock.ADMIN_NOTIFIED)
    assert any(r[0] == "send_message" and r[1] == 7100 for r in fake_bot.records)  # владелец уведомлён
    assert cb.answers and "аблокировано" in cb.answers[-1][0]


async def test_block_device_silent_no_owner_notice(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7101)
    dc = services.add_device(client.id, "d")
    cb, nav = _admin_cb(fake_bot)
    await admin_h.admin_block_do(
        cb, BlockCB(target="dev", action="block", ref=dc.device_id, kind="silent"), services)
    dev = services.db.get_device(dc.device_id)
    assert int(dev.block_reason) & int(DeviceBlock.ADMIN_SILENT)
    assert not any(r[0] == "send_message" and r[1] == 7101 for r in fake_bot.records)  # тихо
    assert "тихо" in cb.answers[-1][0]


# ── блок клиента ─────────────────────────────────────────────────────────────
async def test_block_client_notified_cascades(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7102)
    dc = services.add_device(client.id, "d")
    cb, nav = _admin_cb(fake_bot)
    await admin_h.admin_block_do(
        cb, BlockCB(target="cli", action="block", ref=client.id, kind="notified", days=-1), services)
    fresh = services.db.get_client(client.id)
    dev = services.db.get_device(dc.device_id)
    assert int(fresh.block_reason) & int(ClientBlock.ADMIN_NOTIFIED)
    assert int(dev.block_reason) & int(DeviceBlock.ADMIN_NOTIFIED)
    assert not fresh.is_paused                                # days=-1 → без приостановки


# ── разблокировка: одна причина vs несколько ─────────────────────────────────
async def test_unblock_menu_single_reason_auto(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7103)
    dc = services.add_device(client.id, "d")
    services.block_device_manual(dc.device_id, DeviceBlock.ADMIN_NOTIFIED, notify=False)
    cb, nav = _admin_cb(fake_bot)
    await admin_h.admin_unblock_menu(
        cb, BlockCB(target="dev", action="menu_unblock", ref=dc.device_id), services)
    dev = services.db.get_device(dc.device_id)
    assert int(dev.block_reason) == 0                         # единственную причину сняли сразу
    assert any("азблокировано" in (a[0] or "") for a in cb.answers)


async def test_unblock_menu_multiple_reasons_shows_dialog(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7104)
    dc = services.add_device(client.id, "d")
    services.block_device_manual(dc.device_id, DeviceBlock.ADMIN_SILENT, notify=False)
    services.block_device_manual(dc.device_id, DeviceBlock.USER, notify=False)
    cb, nav = _admin_cb(fake_bot)
    await admin_h.admin_unblock_menu(
        cb, BlockCB(target="dev", action="menu_unblock", ref=dc.device_id), services)
    dev = services.db.get_device(dc.device_id)
    # две причины → диалог выбора, ничего пока не сняли
    assert int(dev.block_reason) & int(DeviceBlock.ADMIN_SILENT)
    assert int(dev.block_reason) & int(DeviceBlock.USER)
    assert any(s[0] == "edit_text" and "снять" in s[1].lower() for s in nav.sent)


async def test_unblock_do_removes_specific_bit(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7105)
    dc = services.add_device(client.id, "d")
    services.block_device_manual(dc.device_id, DeviceBlock.ADMIN_SILENT, notify=False)
    services.block_device_manual(dc.device_id, DeviceBlock.USER, notify=False)
    cb, nav = _admin_cb(fake_bot)
    await admin_h.admin_unblock_do(
        cb, BlockCB(target="dev", action="unblock", ref=dc.device_id, kind="user"), services)
    dev = services.db.get_device(dc.device_id)
    assert int(dev.block_reason) & int(DeviceBlock.USER) == 0        # снят именно USER
    assert int(dev.block_reason) & int(DeviceBlock.ADMIN_SILENT)     # остальное цело
