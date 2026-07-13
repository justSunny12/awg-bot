"""E2E: добор веток роутера админа — понижение лимита с подтверждением, восстановление
устройства (админ), продление, выдача файла, блок с приостановкой (FSM дней),
личные qr/file, выбор устройства для добавления, подключение app-устройства.
"""
import pytest

from awgbot.bot.handlers import admin as ah
from awgbot.bot.callbacks import (BlockCB, ClientCB, ConfirmCB, DeviceCB, Menu, PeriodCB)
from awgbot.core import config
from awgbot.domain import configgen
from awgbot.infra import awg
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


# ── восстановление app-устройства (админ) ────────────────────────────────────
async def test_admin_restore_flow_ok_and_bad(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6302)
    priv = "AP"
    derived = awg.pubkey_of(priv)
    did = services.db.create_device(client.id, "app", derived, "PSK", "10.8.0.80", private_key=None)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.device_restore_start(cb, DeviceCB(action="restore", device_id=did), st, services)
    assert (await st.get_data())["device_id"] == did
    link = configgen.encode_vpn({"containers": [{"awg": {"last_config":
            '{"client_priv_key":"AP","client_pub_key":"x","client_ip":"10.8.0.80"}'}}]})
    await ah.device_restore_apply(_amsg(fake_bot, link), services, st)
    assert services.db.get_device(did).is_managed
    # плохая ссылка
    did2 = services.db.create_device(client.id, "app", "PB2", "PSK", "10.8.0.81", private_key=None)
    st2 = FakeState()
    await st2.update_data(device_id=did2)
    await ah.device_restore_apply(_amsg(fake_bot, "vnp://bad"), services, st2)
    assert services.db.get_device(did2).is_app


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


# ── регресс: ФА-устройство в connect_menu идёт в ВЫДАЧУ ссылки, не в app-диалог ─
async def test_fa_device_connect_menu_routes_to_delivery(services, fake_bot, monkeypatch):
    """Баг: ФА уходил в диалог реставрации вместо выдачи ссылки."""
    from awgbot.core import config
    from awgbot.domain import configgen
    from awgbot.bot.callbacks import DeviceCB
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "p")
    monkeypatch.setattr(config, "BACKUP_ENCRYPTION_ENABLED", True)
    svc = services.db.get_service_client_id()
    did = services.db.create_device(svc, "Admin [x]", "PUBFAX", "PSK", "10.8.1.3")
    monkeypatch.setattr(configgen, "classify_vpn_link",
                        lambda link: {"kind": "full_access", "host": "h", "user": "u"})
    services.attach_full_access(did, "vpn://fa")
    cb, nav = _acb(fake_bot)
    await ah.admin_device_connect_menu(cb, DeviceCB(action="connect_menu", device_id=did), services)
    # должно показать выбор способа выдачи (CONNECT_METHOD_ASK), НЕ app-диалог
    from awgbot.bot import texts
    shown = [r for r in fake_bot.records if r[0] in ("edit_text", "send_message")]
    body = " ".join(str(r) for r in shown)
    assert texts.APP_DEVICE_PICK_DIALOG not in body


async def test_clear_fa_returns_device_to_pool(services, fake_bot, monkeypatch):
    """Снятие метки → устройство снова app-без-клиента (управляемо)."""
    from awgbot.core import config
    from awgbot.domain import configgen
    from awgbot.bot.callbacks import AdminLinkGate
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "p")
    monkeypatch.setattr(config, "BACKUP_ENCRYPTION_ENABLED", True)
    svc = services.db.get_service_client_id()
    did = services.db.create_device(svc, "guest", "PUBG", "PSK", "10.8.1.7")
    monkeypatch.setattr(configgen, "classify_vpn_link",
                        lambda link: {"kind": "full_access", "host": "h", "user": "u"})
    services.attach_full_access(did, "vpn://fa")
    cb, nav = _acb(fake_bot)
    await ah.fa_clear_confirmed(cb, AdminLinkGate(device_id=did, method="clear", confirm=True), services)
    dev = services.db.get_device(did)
    assert dev.is_admin is False and dev.client_id == svc


async def test_client_mode_fa_link_redirects_to_attach(services, fake_bot, monkeypatch):
    """Ревью-фикс: на app-устройстве (mode=client) прислали ФА-ссылку →
    прозрачно уходит в attach, а не в ошибку про несуществующую кнопку."""
    from awgbot.core import config
    from awgbot.domain import configgen
    from awgbot.bot.handlers import admin as ah
    from awgbot.bot.states import RestoreDevice
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "p")
    monkeypatch.setattr(config, "BACKUP_ENCRYPTION_ENABLED", True)
    monkeypatch.setattr(configgen, "classify_vpn_link",
                        lambda link: {"kind": "full_access", "host": "h", "user": "u"})
    svc = services.db.get_service_client_id()
    did = services.db.create_device(svc, "app", "PUBREDIR", "PSK", "10.8.1.4")
    st = FakeState()
    await st.set_state(RestoreDevice.link)
    await st.update_data(device_id=did, mode="client")     # клиентский режим
    await ah.device_restore_apply(_amsg(fake_bot, "vpn://fa"), services, st)
    # ФА-ссылка прошла в attach: устройство стало admin
    assert services.db.get_device(did).is_admin is True


async def test_fa_save_deletes_link_message(services, fake_bot, monkeypatch):
    """После сохранения ФА сообщение пользователя со ссылкой удаляется из чата."""
    from awgbot.core import config
    from awgbot.domain import configgen
    from awgbot.bot.handlers import admin as ah
    from awgbot.bot.states import RestoreDevice
    monkeypatch.setattr(config, "BACKUP_PASSPHRASE", "p")
    monkeypatch.setattr(config, "BACKUP_ENCRYPTION_ENABLED", True)
    monkeypatch.setattr(configgen, "classify_vpn_link",
                        lambda link: {"kind": "full_access", "host": "h", "user": "u"})
    svc = services.db.get_service_client_id()
    did = services.db.create_device(svc, "Admin [x]", "PUBDEL", "PSK", "10.8.1.8")
    st = FakeState()
    await st.set_state(RestoreDevice.link)
    await st.update_data(device_id=did, mode="fa")
    msg = _amsg(fake_bot, "vpn://secret-root-link")
    await ah.device_restore_apply(msg, services, st)
    # сообщение со ссылкой удалено
    assert any(r[0] == "delete" for r in fake_bot.records)
    assert services.db.get_device(did).is_admin is True
