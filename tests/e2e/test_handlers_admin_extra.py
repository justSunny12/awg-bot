"""E2E: добор веток роутера админа — понижение лимита с подтверждением,
продление, выдача файла, блок с приостановкой (FSM дней), личные qr/file,
выбор устройства для добавления, карточка пира без приватного ключа.
"""
import pytest

from awgbot.bot.handlers import admin as ah
from awgbot.bot.callbacks import BlockCB, ClientCB, ConfirmCB, DeviceCB
from awgbot.core import config
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _amsg(bot, text=""):
    return FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=bot)


# ── понижение лимита ниже числа устройств ────────────────────────────────────
async def test_edit_limit_lower_confirm_yes(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6300, device_limit=5)
    services.add_device(client.id, "a")
    services.add_device(client.id, "b")
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.edit_limit_start(cb, ClientCB(action="edit_limit", client_id=client.id), services, st)
    await ah.edit_limit_apply(_amsg(fake_bot, "1"), services, st)   # 1 < 2 → диалог подтверждения
    assert (await st.get_data())["pending_limit"] == 1
    cb2, nav2 = _acb(fake_bot)
    await ah.edit_limit_confirm(cb2, ConfirmCB(action="lower_limit", ref=client.id, yes=True), services, st)
    assert services.db.get_client(client.id).device_limit == 1


async def test_edit_limit_lower_confirm_no(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6301, device_limit=5)
    services.add_device(client.id, "a")
    services.add_device(client.id, "b")
    st = FakeState()
    await st.update_data(client_id=client.id, pending_limit=1)
    cb, nav = _acb(fake_bot)
    await ah.edit_limit_confirm(cb, ConfirmCB(action="lower_limit", ref=client.id, yes=False), services, st)
    assert services.db.get_client(client.id).device_limit == 5   # не изменён


# ── продление / файл ─────────────────────────────────────────────────────────
async def test_extend_start_renders(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6303, period_kind="year")
    cb, nav = _acb(fake_bot)
    await ah.extend_start(cb, ClientCB(action="extend", client_id=client.id), services)
    assert any(s[0] == "edit_text" for s in nav.sent)


async def test_admin_dev_file(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6304)
    dc = services.add_device(client.id, "d")
    cb, nav = _acb(fake_bot)
    await ah.admin_dev_file(cb, DeviceCB(action="gen_file", device_id=dc.device_id), services)
    assert any(s[0] == "document" for s in nav.sent)


# ── блок клиента с приостановкой (FSM дней) ──────────────────────────────────
async def test_block_client_pause_flow(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6305, period_kind="year")
    cb, nav = _acb(fake_bot)
    await ah.admin_block_menu(cb, BlockCB(target="cli", action="menu_block", ref=client.id))
    assert any(s[0] == "edit_text" for s in nav.sent)      # спросили про приостановку
    st = FakeState()
    cb2, nav2 = _acb(fake_bot)
    await ah.admin_block_pause_yes(cb2, BlockCB(target="cli", action="pause_yes", ref=client.id), st)
    assert (await st.get_data())["block_client"] == client.id
    m_days = _amsg(fake_bot, "7")
    await ah.admin_block_pause_days(m_days, services, st)
    assert await st.get_data() == {}                        # FSM закрыт
    assert any(s[0] == "answer" for s in m_days.sent)       # показан выбор уведомления
    # ветка «без приостановки»
    cb3, nav3 = _acb(fake_bot)
    await ah.admin_block_pause_no(cb3, BlockCB(target="cli", action="pause_no", ref=client.id))
    assert any(s[0] == "edit_text" for s in nav3.sent)


async def test_block_menu_device_branch(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6306)
    dc = services.add_device(client.id, "d")
    cb, nav = _acb(fake_bot)
    await ah.admin_block_menu(cb, BlockCB(target="dev", action="menu_block", ref=dc.device_id))
    assert any(s[0] == "edit_text" for s in nav.sent)


# ── личные qr/file, выбор устройства ─────────────────────────────────────────
async def test_self_gen_qr_file_pickers(services, fake_bot):
    services.ensure_admin_client()
    ac = services.admin_client()
    services.add_device(ac.id, "d")
    for handler in (ah.self_gen_qr, ah.self_gen_file):
        cb, nav = _acb(fake_bot)
        await handler(cb, services)
        assert any(s[0] == "edit_text" for s in nav.sent)


async def test_add_device_choice_and_pick(services, fake_bot, make_active_client):
    make_active_client(tg_id=6307)
    cb, nav = _acb(fake_bot)
    await ah.admin_add_device_choice(cb, services)
    assert any(s[0] == "edit_text" for s in nav.sent)
    cb2, nav2 = _acb(fake_bot)
    await ah.admin_add_device_pick(cb2, services)
    assert any(s[0] == "edit_text" for s in nav2.sent)


async def test_admin_menu_devices(services, fake_bot):
    services.ensure_admin_client()
    cb, nav = _acb(fake_bot)
    await ah.admin_menu_devices(cb, services)
    assert any(s[0] == "edit_text" for s in nav.sent)


# ── регресс: у устройства без ключа не должно остаться тупиковых кнопок ───────
async def test_unmanaged_device_offers_no_dead_restore_button(services, fake_bot):
    """Пир, подхваченный с сервера: реставрации больше нет — и кнопок в неё тоже.

    Обработчика action="restore" в боте не осталось. Кнопка, которая на него
    ссылается, молча ничего не делает: нажатие уходит в пустоту, а человек
    остаётся с ощущением сломанного бота. Проверяем оба экрана, где такое
    устройство вообще показывается.
    """
    from awgbot.bot import keyboards as kb

    svc = services.db.get_service_client_id()
    did = services.db.create_device(svc, "Неизвестный пир 10.8.1.9", "PUBQ", "PSK",
                                    "10.8.1.9", private_key=None)
    dev = services.db.get_device(did)
    assert not dev.is_managed

    markups = [kb.device_actions(dev, is_admin=True, back_target="x",
                                 reassign_label="🔀 Передать"),
               kb.unmanaged_device_dialog(did)]
    for m in markups:
        for row in m.inline_keyboard:
            for b in row:
                assert "restore" not in (b.callback_data or ""), b.text


async def test_unmanaged_device_connect_menu_says_there_is_no_link(
        services, fake_bot):
    """Карточка «как подключить» для такого пира честно говорит: ссылки нет."""
    from awgbot.bot import texts

    svc = services.db.get_service_client_id()
    did = services.db.create_device(svc, "чужой", "PUBQ2", "PSK", "10.8.1.10",
                                    private_key=None)
    cb, nav = _acb(fake_bot)
    await ah.admin_device_connect_menu(cb, DeviceCB(action="connect_menu", device_id=did),
                                       services)
    shown = [r[1] for r in nav.sent if r[0] == "edit_text"]
    assert texts.UNMANAGED_DEVICE_DIALOG in shown
