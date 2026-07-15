"""E2E: полный роутер клиента (handlers/client.py) — меню, устройства, выдача
конфигов, добавление/удаление, восстановление app-устройства, самоблок,
приостановка подписки, отсрочка.
"""
import pytest

from awgbot.bot.handlers import client as ch
from awgbot.bot.callbacks import (BlockCB, DelDeviceCB, DeviceCB, GraceCB, Menu, PauseCB)
from awgbot.core.blocks import ClientBlock, DeviceBlock
from awgbot.domain import configgen
from awgbot.infra import awg
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e


def _cb(bot, uid):
    nav = FakeMessage(chat_id=uid, user_id=uid, bot=bot)
    return FakeCallback(message=nav, user_id=uid, bot=bot), nav


def _fresh(services, client):
    return services.db.get_client(client.id)


async def test_menu_main_and_info_and_devices(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5000, period_kind="year")
    services.add_device(client.id, "d")
    cl = _fresh(services, client)
    for handler in (ch.menu_main, ch.menu_info, ch.menu_devices):
        cb, nav = _cb(fake_bot, 5000)
        await handler(cb, cl, services)
        assert cb.answers


async def test_menu_gen_empty_vs_present(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5001)
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5001)
    await ch.menu_gen_link(cb, cl, services)
    assert cb.answers[-1][1] is True
    services.add_device(client.id, "d")
    cb2, nav2 = _cb(fake_bot, 5001)
    await ch.menu_gen_link(cb2, cl, services)
    assert any(s[0] == "edit_text" for s in nav2.sent)


async def test_device_open_own_foreign_app(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5002)
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5002)
    await ch.device_open(cb, DeviceCB(action="open", device_id=dc.device_id), cl, services)
    assert any(s[0] == "edit_text" for s in nav.sent)
    cb2, nav2 = _cb(fake_bot, 5002)
    await ch.device_open(cb2, DeviceCB(action="open", device_id=999999), cl, services)
    assert cb2.answers[-1][1] is True
    app_id = services.db.create_device(client.id, "app", "PUBZ", "PSK", "10.8.0.60", private_key=None)
    cb3, nav3 = _cb(fake_bot, 5002)
    await ch.device_open(cb3, DeviceCB(action="open", device_id=app_id), cl, services)
    assert any(s[0] == "edit_text" for s in nav3.sent)


async def test_device_connect_menu_bot_vs_app(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5003)
    dc = services.add_device(client.id, "d")
    app_id = services.db.create_device(client.id, "app", "PUBY", "PSK", "10.8.0.61", private_key=None)
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5003)
    await ch.device_connect_menu(cb, DeviceCB(action="connect_menu", device_id=dc.device_id), cl, services)
    assert any(s[0] == "edit_text" for s in nav.sent)
    cb2, nav2 = _cb(fake_bot, 5003)
    await ch.device_connect_menu(cb2, DeviceCB(action="connect_menu", device_id=app_id), cl, services)
    assert any(s[0] == "edit_text" for s in nav2.sent)


async def test_gen_from_menu_bot_sends_config(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5004)
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5004)
    await ch.device_gen_link(cb, DeviceCB(action="gen_link", device_id=dc.device_id), cl, services)
    assert any(s[0] == "answer" for s in nav.sent)


async def test_gen_from_menu_app_shows_dialog(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5005)
    app_id = services.db.create_device(client.id, "app", "PUBW", "PSK", "10.8.0.62", private_key=None)
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5005)
    await ch.device_gen_qr(cb, DeviceCB(action="gen_qr", device_id=app_id), cl, services)
    assert any(s[0] == "edit_text" for s in nav.sent)


async def test_edit_device_traffic_flow(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5006)
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    st = FakeState()
    cb, nav = _cb(fake_bot, 5006)
    await ch.client_edit_device_traffic(cb, DeviceCB(action="edit_traffic", device_id=dc.device_id),
                                        cl, services, st)
    assert (await st.get_data())["ref"] == dc.device_id
    m_bad = FakeMessage(text="abc", chat_id=5006, user_id=5006, bot=fake_bot)
    await ch.client_edit_traffic_apply(m_bad, cl, services, st)
    assert any(s[0] == "answer" for s in m_bad.sent)
    m_ok = FakeMessage(text="10", chat_id=5006, user_id=5006, bot=fake_bot)
    await ch.client_edit_traffic_apply(m_ok, cl, services, st)
    assert services.db.get_device(dc.device_id).traffic_limit == 10 * (1024 ** 3)


async def test_transfer_and_reinvite(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5007)
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5007)
    await ch.device_transfer_do(cb, DeviceCB(action="transfer_yes", device_id=dc.device_id), cl, services)
    assert services.db.get_device(dc.device_id).friend_status == "pending"
    cb2, nav2 = _cb(fake_bot, 5007)
    await ch.device_reinvite(cb2, DeviceCB(action="reinvite", device_id=dc.device_id), cl, services)
    assert cb2.answers


async def test_restore_app_device_flow(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5008)
    priv = "RP"
    derived = awg.pubkey_of(priv)
    did = services.db.create_device(client.id, "app", derived, "PSK", "10.8.0.63", private_key=None)
    cl = _fresh(services, client)
    st = FakeState()
    cb, nav = _cb(fake_bot, 5008)
    await ch.device_restore_start(cb, DeviceCB(action="restore", device_id=did), cl, services, st)
    assert (await st.get_data())["device_id"] == did
    obj = {"containers": [{"awg": {"last_config":
           '{"client_priv_key":"RP","client_pub_key":"x","client_ip":"10.8.0.63"}'}}]}
    link = configgen.encode_vpn(obj)
    m = FakeMessage(text=link, chat_id=5008, user_id=5008, bot=fake_bot)
    await ch.device_restore_apply(m, cl, services, st)
    assert services.db.get_device(did).is_managed


async def test_restore_bad_link(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5009)
    did = services.db.create_device(client.id, "app", "PB", "PSK", "10.8.0.64", private_key=None)
    cl = _fresh(services, client)
    st = FakeState()
    await st.update_data(device_id=did)
    m = FakeMessage(text="vnp://garbage", chat_id=5009, user_id=5009, bot=fake_bot)
    await ch.device_restore_apply(m, cl, services, st)
    assert services.db.get_device(did).is_app


async def test_add_device_self_full_flow(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5010, device_limit=3)
    cl = _fresh(services, client)
    st = FakeState()
    cb, nav = _cb(fake_bot, 5010)
    await ch.device_add_self(cb, cl, services, st)
    m_name = FakeMessage(text="Мой ноут", chat_id=5010, user_id=5010, bot=fake_bot)
    await ch.device_add_name(m_name, cl, services, st)
    m_tr = FakeMessage(text="0", chat_id=5010, user_id=5010, bot=fake_bot)
    await ch.device_add_traffic(m_tr, cl, services, st)
    assert any(d.name == "Мой ноут" for d in services.db.list_devices(client.id))


async def test_add_device_name_empty_rejected(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5011)
    cl = _fresh(services, client)
    st = FakeState()
    await st.update_data(for_friend=False)
    m = FakeMessage(text="   ", chat_id=5011, user_id=5011, bot=fake_bot)
    await ch.device_add_name(m, cl, services, st)
    assert any(s[0] == "answer" for s in m.sent)


async def test_add_device_start_when_full_shows_delete(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5012, device_limit=1)
    services.add_device(client.id, "occupied")
    cl = _fresh(services, client)
    st = FakeState()
    cb, nav = _cb(fake_bot, 5012)
    await ch.device_add_start(cb, cl, services, st)
    assert any(s[0] == "edit_text" for s in nav.sent)


async def test_add_device_for_friend_flow(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5013, device_limit=3)
    cl = _fresh(services, client)
    st = FakeState()
    await st.update_data(for_friend=True, dev_name="ДругНоут")
    m_tr = FakeMessage(text="5", chat_id=5013, user_id=5013, bot=fake_bot)
    await ch.device_add_traffic(m_tr, cl, services, st)
    dev = [d for d in services.db.list_devices(client.id) if d.name == "ДругНоут"][0]
    assert dev.friend_status == "pending"


async def test_delete_device_flow(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5014)
    services.add_device(client.id, "a")
    d2 = services.add_device(client.id, "b")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5014)
    await ch.device_delete_ask(cb, DelDeviceCB(device_id=d2.device_id, stage="ask"), cl, services)
    assert any(s[0] == "edit_text" for s in nav.sent)
    cb2, nav2 = _cb(fake_bot, 5014)
    await ch.device_delete_confirm(cb2, DelDeviceCB(device_id=d2.device_id, stage="confirm"), cl, services)
    assert services.db.get_device(d2.device_id) is None


async def test_client_block_unblock_own_device(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5015)
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5015)
    await ch.client_block_device(cb, BlockCB(target="dev", action="menu_block", ref=dc.device_id), cl, services)
    assert int(services.db.get_device(dc.device_id).block_reason) & int(DeviceBlock.USER)
    cb2, nav2 = _cb(fake_bot, 5015)
    await ch.client_unblock_device(cb2, BlockCB(target="dev", action="menu_unblock", ref=dc.device_id), cl, services)
    assert int(services.db.get_device(dc.device_id).block_reason) & int(DeviceBlock.USER) == 0


async def test_client_unblock_when_not_user_blocked(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5016)
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5016)
    await ch.client_unblock_device(cb, BlockCB(target="dev", action="menu_unblock", ref=dc.device_id), cl, services)
    assert cb.answers[-1][1] is True


async def test_pause_full_cycle(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5017, period_kind="year")
    services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5017)
    await ch.pause_ask(cb, PauseCB(action="ask", ref=client.id), cl, services)
    assert any(s[0] == "edit_text" for s in nav.sent)
    cb2, nav2 = _cb(fake_bot, 5017)
    await ch.pause_confirm(cb2, PauseCB(action="confirm", ref=client.id), cl, services)
    assert _fresh(services, client).is_paused
    cl2 = _fresh(services, client)
    cb3, nav3 = _cb(fake_bot, 5017)
    await ch.pause_resume(cb3, PauseCB(action="resume", ref=client.id), cl2, services)
    assert not _fresh(services, client).is_paused


async def test_pause_ask_unavailable(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5018, period_kind="month")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5018)
    await ch.pause_ask(cb, PauseCB(action="ask", ref=client.id), cl, services)
    assert cb.answers[-1][1] is True


async def test_resume_guard_blocks_non_user_pause(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5019, period_kind="year")
    services.enter_admin_pause(client.id, 0)
    services._client_set_block(client.id, ClientBlock.PAUSED)
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5019)
    await ch.pause_resume(cb, PauseCB(action="resume", ref=client.id), cl, services)
    assert cb.answers[-1][1] is True
    assert _fresh(services, client).is_paused


async def test_grace_take_happy_and_stale_ref(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5020, period_kind="year")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5020)
    await ch.grace_take(cb, GraceCB(action="take", ref=999999), cl, services)
    assert cb.answers[-1][1] is True
    cb2, nav2 = _cb(fake_bot, 5020)
    await ch.grace_take(cb2, GraceCB(action="take", ref=client.id), cl, services)
    assert _fresh(services, client).grace_used == 1


async def test_help_root_and_skip(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5021)
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5021)
    await ch.help_root(cb)
    assert any(s[0] == "edit_text" for s in nav.sent)
    cb2, nav2 = _cb(fake_bot, 5021)
    await ch.help_skip(cb2, cl, services)
    assert cb2.answers


async def test_client_renames_own_device(services, fake_bot, make_active_client, monkeypatch):
    """Клиент переименовывает СВОЁ устройство → rename_device (обе базы)."""
    from awgbot.bot.handlers import client as ch
    from awgbot.bot.callbacks import DeviceCB
    from awgbot.infra import awg
    monkeypatch.setattr(awg, "clientstable_upsert", lambda pub, name: None)
    cl = make_active_client(tg_id=7001, name="Клиент")
    dc = services.add_device(cl.id, "Старое")
    cb, nav = _cb(fake_bot, cl.tg_id)
    st = FakeState()
    await ch.client_device_edit_name_start(cb, DeviceCB(action="edit_name", device_id=dc.device_id),
                                            cl, services, st)
    msg = FakeMessage(text="Новое", chat_id=cl.tg_id, user_id=cl.tg_id, bot=fake_bot)
    await ch.client_device_edit_name_apply(msg, cl, services, st)
    assert services.db.get_device(dc.device_id).name == "Новое"


async def test_client_cannot_rename_foreign_device(services, fake_bot, make_active_client, monkeypatch):
    """Чужое устройство (другого клиента) переименовать нельзя — own_device режет."""
    from awgbot.bot.handlers import client as ch
    from awgbot.bot.callbacks import DeviceCB
    owner = make_active_client(tg_id=7002, name="Владелец")
    other = make_active_client(tg_id=7003, name="Чужой")
    dc = services.add_device(owner.id, "ЧужоеУстройство")
    cb, nav = _cb(fake_bot, other.tg_id)
    st = FakeState()
    # 'other' пытается открыть переименование чужого устройства
    await ch.client_device_edit_name_start(cb, DeviceCB(action="edit_name", device_id=dc.device_id),
                                            other, services, st)
    # own_device вернул None → ранний выход, device_id в state не записан
    assert "device_id" not in (await st.get_data())
    assert services.db.get_device(dc.device_id).name == "ЧужоеУстройство"
