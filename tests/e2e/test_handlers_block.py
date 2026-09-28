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


# ── блокировка профиля: пауза до снятия, без выбора срока ───────────────────

def _labels(nav):
    shown = [s for s in nav.sent if s[0] == "edit_text"]
    assert shown, "экран не отрисован"
    return shown[-1][1], [b.text for row in shown[-1][2].inline_keyboard for b in row]


async def _pick(services, bot, client_id, pause: str):
    """«🛑 Блок» профиля → «⏸️ Да» / «▶️ Нет» → экран «Уведомить владельца?»:
    возвращает кнопки второго шага."""
    cb, nav = _admin_cb(bot)
    await admin_h.admin_block_menu(cb, BlockCB(target="cli", action="menu_block", ref=client_id), services)
    text, labels = _labels(nav)
    assert text.split("\n")[1] == "Приостановить подписку на время блокировки?", text
    assert labels == ["⏸️ Да", "▶️ Нет", "⬅️ Отмена"], labels
    step = admin_h.admin_block_pause_yes if pause == "yes" else admin_h.admin_block_pause_no
    cb2, nav2 = _admin_cb(bot)
    await step(cb2, BlockCB(target="cli", action=f"pause_{pause}", ref=client_id))
    text2, labels2 = _labels(nav2)
    assert text2 == "Уведомить владельца профиля?", text2
    assert labels2 == ["🔔 Да", "🔕 Нет", "⬅️ Отмена"], labels2
    markup = [s for s in nav2.sent if s[0] == "edit_text"][-1][2]
    return [b.callback_data for row in markup.inline_keyboard for b in row]


async def test_block_profile_with_pause_pauses_until_unblocked(services, fake_bot, make_active_client):
    """«⏸️ Да» → «🔔 Да»: профиль заблокирован и подписка приостановлена до
    снятия блокировки (бессрочная админская пауза) — ни шага ввода дней, ни
    выбора срока. Снятие блокировки закрывает паузу, срок подписки
    возвращается. Пауза «на 7 дней» вместо «до снятия» оживила бы подписку
    посреди блокировки."""
    from awgbot.core.enums import PauseMode
    client = make_active_client(tg_id=7110, period_kind="year")
    dc = services.add_device(client.id, "d")
    notify_yes, _, _ = await _pick(services, fake_bot, client.id, "yes")
    cb, nav = _admin_cb(fake_bot)
    await admin_h.admin_block_do(cb, BlockCB.unpack(notify_yes), services)
    fresh = services.db.get_client(client.id)
    assert int(fresh.block_reason) & int(ClientBlock.ADMIN_NOTIFIED)
    assert fresh.is_paused and fresh.pause_mode == PauseMode.ADMIN_OPEN, fresh.pause_mode
    assert int(services.db.get_device(dc.device_id).block_reason) & int(DeviceBlock.PAUSED)
    assert any(r[0] == "send_message" and r[1] == 7110 for r in fake_bot.records), "владелец не уведомлён"
    text, _ = _labels(nav)
    assert text.startswith("👤 ") and "⛔ Заблокирован: администратором" in text, text
    assert cb.answers[-1][0] == f"🛑 Профиль {client.name} заблокирован"

    cb2, _ = _admin_cb(fake_bot)
    await admin_h.admin_unblock_menu(cb2, BlockCB(target="cli", action="menu_unblock", ref=client.id), services)
    back = services.db.get_client(client.id)
    assert int(back.block_reason) == 0 and not back.is_paused, "снятие блокировки не закрыло паузу"
    assert back.period_end, "срок подписки не вернулся после паузы"


async def test_block_profile_without_pause_and_silently(services, fake_bot, make_active_client):
    """«▶️ Нет» → «🔕 Нет»: блок без паузы и без уведомления владельцу."""
    client = make_active_client(tg_id=7111, period_kind="year")
    _, notify_no, cancel = await _pick(services, fake_bot, client.id, "no")
    cb, _ = _admin_cb(fake_bot)
    await admin_h.admin_block_do(cb, BlockCB.unpack(notify_no), services)
    fresh = services.db.get_client(client.id)
    assert int(fresh.block_reason) & int(ClientBlock.ADMIN_SILENT)
    assert not fresh.is_paused, "пауза без просьбы"
    assert not any(r[0] == "send_message" and r[1] == 7111 for r in fake_bot.records)
    assert cb.answers[-1][0] == f"🛑 Профиль {client.name} заблокирован (тихо)"
    assert BlockCB.unpack(cancel).action == "cancel"


async def test_block_profile_cancel_changes_nothing(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=7112)
    _, _, cancel = await _pick(services, fake_bot, client.id, "yes")
    cb, nav = _admin_cb(fake_bot)
    await admin_h.admin_block_cancel(cb, BlockCB.unpack(cancel), services)
    assert int(services.db.get_client(client.id).block_reason) == 0
    assert _labels(nav)[0].startswith("👤 "), "отмена не вернула в карточку"
