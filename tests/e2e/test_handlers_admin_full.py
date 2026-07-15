"""E2E: роутер администратора (handlers/admin.py) — панель, создание/правка/
удаление клиента, устройства клиента, перепривязка app-устройства, реставрация,
бэкап, перезапуск, личный VPN админа.

Не дублирует уже покрытое в test_handlers_block / test_handlers_extend.
"""
import pytest

from awgbot.bot.handlers import admin as ah
from awgbot.bot.callbacks import (AdminSelfCB, ClientCB, ConfirmCB, DelDeviceCB, DeviceCB,
                                  Menu, PeriodCB, ReassignCB)
from awgbot.core import config
from awgbot.util import secrets_util
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _amsg(bot, text=""):
    return FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=bot)


# ── панель и список клиентов ─────────────────────────────────────────────────
async def test_panel_and_clients_list(services, fake_bot, make_active_client):
    make_active_client(tg_id=6000, name="Клиент")
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.admin_main_menu(cb, services, st)
    assert any(s[0] == "edit_text" for s in nav.sent)
    cb2, nav2 = _acb(fake_bot)
    await ah.clients_list(cb2, services)
    assert any(s[0] == "edit_text" for s in nav2.sent)


async def test_client_open_card(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6001)
    cb, nav = _acb(fake_bot)
    await ah.client_open(cb, ClientCB(action="open", client_id=client.id), services)
    assert any(s[0] == "edit_text" for s in nav.sent)


# ── создание клиента (FSM) ───────────────────────────────────────────────────
async def test_create_client_full_flow(services, fake_bot):
    services.ensure_admin_client()
    st = FakeState()
    await ah.add_client_name(_amsg(fake_bot, "Вася"), services, st)
    await ah.add_client_limit(_amsg(fake_bot, "3"), services, st)
    await ah.add_client_traffic(_amsg(fake_bot, "100"), services, st)
    cb, nav = _acb(fake_bot)
    before = len(services.db.list_clients())
    await ah.add_client_period(cb, PeriodCB(kind="year", ctx="create"), services, st)
    assert len(services.db.list_clients()) == before + 1


async def test_create_client_bad_inputs(services, fake_bot):
    st = FakeState()
    m = _amsg(fake_bot, "   ")
    await ah.add_client_name(m, services, st)
    assert any(s[0] == "answer" for s in m.sent)             # пустое имя отклонено
    await st.update_data(name="X")
    m2 = _amsg(fake_bot, "abc")
    await ah.add_client_limit(m2, services, st)
    assert any(s[0] == "answer" for s in m2.sent)            # нечисловой лимит отклонён


async def test_create_client_stale_dialog(services, fake_bot):
    st = FakeState()                                         # пустой FSM
    cb, nav = _acb(fake_bot)
    await ah.add_client_period(cb, PeriodCB(kind="year", ctx="create"), services, st)
    assert cb.answers[-1][1] is True                        # «диалог устарел»


# ── добавление устройства клиенту (FSM) ──────────────────────────────────────
async def test_admin_add_device_for_client(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6002, device_limit=3)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.admin_add_device_start(cb, ClientCB(action="add_device", client_id=client.id), services, st)
    await ah.admin_add_device_name(_amsg(fake_bot, "Дев"), services, st)
    await ah.admin_add_device_traffic(_amsg(fake_bot, "0"), services, st)
    assert any(d.name == "Дев" for d in services.db.list_devices(client.id))


# ── правка имени / лимита / трафика ──────────────────────────────────────────
async def test_edit_name_flow(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6003)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.edit_name_start(cb, ClientCB(action="edit_name", client_id=client.id), services, st)
    await ah.edit_name_apply(_amsg(fake_bot, "НовоеИмя"), services, st)
    assert services.db.get_client(client.id).name == "НовоеИмя"


async def test_edit_client_traffic_flow(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6004)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.edit_client_traffic_start(cb, ClientCB(action="edit_traffic", client_id=client.id), services, st)
    await ah.edit_traffic_apply(_amsg(fake_bot, "50"), services, st)
    assert services.db.get_client(client.id).traffic_limit == 50 * (1024 ** 3)


async def test_edit_limit_raise(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6005, device_limit=2)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.edit_limit_start(cb, ClientCB(action="edit_limit", client_id=client.id), services, st)
    await ah.edit_limit_apply(_amsg(fake_bot, "5"), services, st)
    assert services.db.get_client(client.id).device_limit == 5


# ── инвайт / удаление ────────────────────────────────────────────────────────
async def test_regen_invite(services, fake_bot):
    services.ensure_admin_client()
    created = services.create_client("Пендинг", 3, "year", 0)   # pending, инвайт можно перевыпустить
    old = created.invite_code
    cb, nav = _acb(fake_bot)
    await ah.regen_invite(cb, ClientCB(action="regen_invite", client_id=created.client_id), services)
    assert services.db.get_client(created.client_id).invite_code != old


async def test_delete_client_flow(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6007)
    cb, nav = _acb(fake_bot)
    await ah.client_delete_confirm(cb, ClientCB(action="delete", client_id=client.id), services)
    assert any(s[0] == "edit_text" for s in nav.sent)
    cb2, nav2 = _acb(fake_bot)
    await ah.client_delete_apply(cb2, ConfirmCB(action="del_client", ref=client.id, yes=True), services)
    assert services.db.get_client(client.id) is None


# ── выдача конфигов клиенту / устройству ─────────────────────────────────────
async def test_admin_gen_for_and_client_devices(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6008)
    services.add_device(client.id, "d")
    cb, nav = _acb(fake_bot)
    await ah.admin_gen_for(cb, ClientCB(action="gen_for", client_id=client.id), services)
    assert any(s[0] == "edit_text" for s in nav.sent)
    cb2, nav2 = _acb(fake_bot)
    await ah.admin_client_devices(cb2, ClientCB(action="devices", client_id=client.id), services)
    assert any(s[0] == "edit_text" for s in nav2.sent)


async def test_admin_dev_link_and_qr(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6009)
    dc = services.add_device(client.id, "d")
    cb, nav = _acb(fake_bot)
    await ah.admin_dev_link(cb, DeviceCB(action="gen_link", device_id=dc.device_id), services)
    assert any(s[0] == "answer" for s in nav.sent)
    cb2, nav2 = _acb(fake_bot)
    await ah.admin_dev_qr(cb2, DeviceCB(action="gen_qr", device_id=dc.device_id), services)
    assert any(s[0] == "animation" for s in nav2.sent)


async def test_admin_device_open_and_connect(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6010)
    dc = services.add_device(client.id, "d")
    cb, nav = _acb(fake_bot)
    await ah.admin_device_open(cb, DeviceCB(action="open", device_id=dc.device_id), services)
    assert any(s[0] == "edit_text" for s in nav.sent)
    cb2, nav2 = _acb(fake_bot)
    await ah.admin_device_connect_menu(cb2, DeviceCB(action="connect_menu", device_id=dc.device_id), services)
    assert any(s[0] == "edit_text" for s in nav2.sent)


# ── перепривязка app-устройства ──────────────────────────────────────────────
async def test_reassign_flow_with_slot(services, fake_bot, make_active_client):
    a = make_active_client(tg_id=6011, device_limit=3)
    b = make_active_client(tg_id=6012, device_limit=3)
    dc = services.add_device(a.id, "d")
    cb, nav = _acb(fake_bot)
    await ah.device_reassign_start(cb, DeviceCB(action="reassign", device_id=dc.device_id), services)
    assert any(s[0] == "edit_text" for s in nav.sent)
    cb2, nav2 = _acb(fake_bot)
    await ah.device_reassign_apply(cb2, ReassignCB(device_id=dc.device_id, client_id=b.id, stage="go"), services)
    assert services.db.get_device(dc.device_id).client_id == b.id


async def test_reassign_no_slot_prompts_then_slot_yes(services, fake_bot, make_active_client):
    a = make_active_client(tg_id=6013, device_limit=3)
    b = make_active_client(tg_id=6014, device_limit=1)
    services.add_device(b.id, "occupied")
    dc = services.add_device(a.id, "d")
    cb, nav = _acb(fake_bot)
    await ah.device_reassign_apply(cb, ReassignCB(device_id=dc.device_id, client_id=b.id, stage="go"), services)
    assert any(s[0] == "edit_text" and "слот" in s[1].lower() for s in nav.sent)
    cb2, nav2 = _acb(fake_bot)
    await ah.device_reassign_slot_yes(cb2, ReassignCB(device_id=dc.device_id, client_id=b.id, stage="slot_yes"), services)
    assert services.db.get_device(dc.device_id).client_id == b.id
    assert services.db.get_client(b.id).device_limit == 2


async def test_reassign_slot_no_aborts(services, fake_bot, make_active_client):
    a = make_active_client(tg_id=6015, device_limit=3)
    b = make_active_client(tg_id=6016, device_limit=1)
    services.add_device(b.id, "occupied")
    dc = services.add_device(a.id, "d")
    cb, nav = _acb(fake_bot)
    await ah.device_reassign_slot_no(cb, ReassignCB(device_id=dc.device_id, client_id=b.id, stage="slot_no"), services)
    assert services.db.get_device(dc.device_id).client_id == a.id   # не перепривязано


# ── бэкап / перезапуск (переехали в ⚙️ Настройки) ────────────────────────────
async def test_backup_now_sends_files(services, fake_bot, monkeypatch, tmp_path):
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    f = tmp_path / "bot_backup.db"
    f.write_bytes(b"x")
    monkeypatch.setattr(services, "make_backup", lambda: [str(f)])
    cb, nav = _acb(fake_bot)
    await sh.do_action(cb, SetCB(sec="backup", act="do", key="now"), services)
    assert any(s[0] == "document" for s in nav.sent)


async def test_restart_awg_from_settings(services, fake_bot, monkeypatch):
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    monkeypatch.setattr(services, "restart_service", lambda: None)
    cb, nav = _acb(fake_bot)
    await sh.do_action(cb, SetCB(sec="svc", act="do", key="awg"), services)
    assert any(s[0] == "edit_text" for s in nav.sent)


# ── личный VPN админа ────────────────────────────────────────────────────────
async def test_admin_self_devices_and_add(services, fake_bot):
    services.ensure_admin_client()
    cb, nav = _acb(fake_bot)
    await ah.self_devices(cb, services)
    assert any(s[0] == "edit_text" for s in nav.sent)
    st = FakeState()
    cb2, nav2 = _acb(fake_bot)
    await ah.self_add_start(cb2, services, st)
    await ah.self_add_name(_amsg(fake_bot, "МойДев"), services, st)
    await ah.self_add_traffic(_amsg(fake_bot, "0"), services, st)
    ac = services.admin_client()
    assert any(d.name == "МойДев" for d in services.db.list_devices(ac.id))


async def test_admin_self_gen_link(services, fake_bot):
    services.ensure_admin_client()
    ac = services.admin_client()
    services.add_device(ac.id, "d")
    cb, nav = _acb(fake_bot)
    await ah.self_gen_link(cb, services)
    assert any(s[0] == "edit_text" for s in nav.sent)       # пикер устройства, не прямая ссылка


# ── удаление устройства (админ) ──────────────────────────────────────────────
async def test_admin_delete_device(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6017)
    d1 = services.add_device(client.id, "a")
    d2 = services.add_device(client.id, "b")
    cb, nav = _acb(fake_bot)
    await ah.admin_del_ask(cb, DelDeviceCB(device_id=d2.device_id, stage="ask"), services)
    assert any(s[0] == "edit_text" for s in nav.sent)
    cb2, nav2 = _acb(fake_bot)
    await ah.admin_del_confirm(cb2, DelDeviceCB(device_id=d2.device_id, stage="confirm"), services)
    assert services.db.get_device(d2.device_id) is None


# ── unassigned / add-device choice ───────────────────────────────────────────
async def test_unassigned_list_and_choice(services, fake_bot):
    svc = services.db.get_service_client_id()
    services.db.create_device(svc, "app", "PUBU", "PSK", "10.8.0.70")
    cb, nav = _acb(fake_bot)
    await ah.unassigned_list(cb, services)
    assert any(s[0] == "edit_text" for s in nav.sent)
