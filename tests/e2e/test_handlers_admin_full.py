"""E2E: роутер администратора (handlers/admin.py) — панель, создание/правка/
удаление клиента, устройства клиента, перепривязка устройства без профиля,
бэкап, перезапуск, личный VPN админа.

Не дублирует уже покрытое в test_handlers_block / test_handlers_extend.
"""
import pytest

from awgbot.bot.handlers import admin as ah
from awgbot.bot.callbacks import AdminSelfCB, ClientCB, ConfirmCB, DelDeviceCB, DeviceCB, ReassignCB
from awgbot.core import config
from tests.conftest import FakeCallback, FakeMessage, FakeState, last_screen

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _amsg(bot, text=""):
    return FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=bot)


# ── создание клиента (FSM) ───────────────────────────────────────────────────
async def test_create_client_bad_inputs(services, fake_bot):
    st = FakeState()
    m = _amsg(fake_bot, "   ")
    await ah.add_client_name(m, services, st)
    assert "name" not in await st.get_data()                 # пустое имя не принято
    assert any(s[0] == "answer" for s in m.sent)
    await st.update_data(name="X")
    m2 = _amsg(fake_bot, "abc")
    await ah.add_client_limit(m2, services, st)
    assert "limit" not in await st.get_data()                # нечисловой лимит не принят
    assert any("число" in s[1].lower() for s in m2.sent)


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
async def test_delete_client_asks_before_deleting(services, fake_bot, make_active_client):
    """Кнопка «Удалить» открывает подтверждение и ничего не удаляет сама;
    само удаление — в test_handlers_admin.py."""
    client = make_active_client(tg_id=6007)
    cb, nav = _acb(fake_bot)
    await ah.client_delete_confirm(cb, ClientCB(action="delete", client_id=client.id), services)
    shown = [s for s in nav.sent if s[0] == "edit_text"]
    assert shown and shown[-1][2] is not None, "подтверждение без кнопок — тупик"
    assert services.db.get_client(client.id) is not None


# ── выдача конфигов клиенту / устройству ─────────────────────────────────────
async def test_admin_gen_for_and_client_devices(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6008)
    services.add_device(client.id, "d")
    cb, nav = _acb(fake_bot)
    await ah.admin_gen_for(cb, ClientCB(action="gen_for", client_id=client.id), services)
    _, labels = last_screen(nav)
    assert any("d" in l for l in labels), "устройство не предложено к выдаче"
    cb2, nav2 = _acb(fake_bot)
    await ah.admin_client_devices(cb2, ClientCB(action="devices", client_id=client.id), services)
    _, labels2 = last_screen(nav2)
    assert any("d" in l for l in labels2), "устройства профиля не показаны"


async def test_admin_dev_link_and_qr(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6009)
    dc = services.add_device(client.id, "d")
    cb, nav = _acb(fake_bot)
    await ah.admin_dev_gen(cb, DeviceCB(action="gen_link", device_id=dc.device_id), services)
    assert any(s[0] == "answer" for s in nav.sent)
    cb2, nav2 = _acb(fake_bot)
    await ah.admin_dev_gen(cb2, DeviceCB(action="gen_qr", device_id=dc.device_id), services)
    assert any(s[0] == "animation" for s in nav2.sent)


async def test_admin_device_open_and_connect(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6010)
    dc = services.add_device(client.id, "d")
    cb, nav = _acb(fake_bot)
    await ah.admin_device_open(cb, DeviceCB(action="open", device_id=dc.device_id), services)
    text, labels = last_screen(nav)
    assert "d" in text and any("Данные для подключения" in l for l in labels)
    cb2, nav2 = _acb(fake_bot)
    await ah.admin_device_connect_menu(cb2, DeviceCB(action="connect_menu", device_id=dc.device_id), services)
    _, labels2 = last_screen(nav2)
    assert any("сылк" in l for l in labels2) and any("QR" in l for l in labels2)


# ── перепривязка устройства без профиля ──────────────────────────────────────
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
    restarted = []
    monkeypatch.setattr(services, "restart_service", lambda: restarted.append(1))
    cb, nav = _acb(fake_bot)
    # кнопка сама ничего не рвёт — сначала подтверждение с ценой
    await sh.do_action(cb, SetCB(sec="svc", act="do", key="awg"), services)
    assert restarted == []
    assert any(s[0] == "edit_text" and "Перезапустить AWG?" in s[1] for s in nav.sent)
    await sh.do_action(cb, SetCB(sec="svc", act="do", key="awg!"), services)
    assert restarted == [1]
    assert any(s[0] == "edit_text" and "перезапущен" in s[1] for s in nav.sent)


async def test_restart_bot_from_settings_needs_confirmation(services, fake_bot, monkeypatch):
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    restarted = []
    monkeypatch.setattr(services, "restart_bot", lambda: restarted.append(1))
    cb, nav = _acb(fake_bot)
    await sh.do_action(cb, SetCB(sec="svc", act="do", key="bot"), services)
    assert restarted == [] and services.db.get_state("restart_wait") in (None, "")
    assert any(s[0] == "edit_text" and "Перезапустить бота?" in s[1] for s in nav.sent)
    await sh.do_action(cb, SetCB(sec="svc", act="do", key="bot!"), services)
    assert restarted == [1] and services.db.get_state("restart_wait")


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
    await ah.self_gen_pick(cb, AdminSelfCB(action="gen_link"), services)
    assert any(s[0] == "edit_text" for s in nav.sent)       # пикер устройства, не прямая ссылка


# ── удаление устройства (админ) ──────────────────────────────────────────────
async def test_admin_delete_device(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6017)
    d1 = services.add_device(client.id, "a")
    d2 = services.add_device(client.id, "b")
    cb, nav = _acb(fake_bot)
    await ah.admin_del_ask(cb, DelDeviceCB(device_id=d2.device_id, stage="ask"), services)
    assert any(s[0] == "edit_text" for s in nav.sent)
    assert services.db.get_device(d2.device_id) is not None, "вопрос ещё ничего не удаляет"
    cb2, nav2 = _acb(fake_bot)
    await ah.admin_del_confirm(cb2, DelDeviceCB(device_id=d2.device_id, stage="confirm"), services)
    assert services.db.get_device(d2.device_id) is None
    assert services.db.get_device(d1.device_id) is not None, "соседнее устройство цело"


# ── unassigned / add-device choice ───────────────────────────────────────────
async def test_unassigned_list_and_choice(services, fake_bot):
    svc = services.db.get_service_client_id()
    services.db.create_device(svc, "app", "PUBU", "PSK", "10.8.0.70")
    cb, nav = _acb(fake_bot)
    await ah.unassigned_list(cb, services)
    _, labels = last_screen(nav)
    assert any("app" in l for l in labels), "устройство без профиля не показано"


async def test_delete_client_keeps_profile_when_peer_stays_on_server(
        services, fake_bot, make_active_client, monkeypatch):
    """Пир не снялся — профиль обязан остаться, и об этом надо сказать.

    Прежде отказ remove_peer глотался, запись клиента удалялась каскадом, а
    админу докладывалось «удалено, устройств: N». Итог: VPN у человека
    продолжает работать, а записи, по которой его можно найти, больше нет.
    Соседний поток (удаление ОДНОГО устройства) на том же отказе
    останавливается — расхождение и было ошибкой.
    """
    from awgbot.infra import awg

    client = make_active_client(tg_id=6099)
    dc = services.add_device(client.id, "телефон")
    monkeypatch.setattr(awg, "remove_peer",
                        lambda pub, iface=None: (_ for _ in ()).throw(awg.AwgError("awg не отвечает")))

    cb, nav = _acb(fake_bot)
    await ah.client_delete_apply(
        cb, ConfirmCB(action="del_client", ref=client.id, yes=True), services)

    assert services.db.get_client(client.id) is not None, "профиль удалён вопреки отказу"
    assert services.db.get_device(dc.device_id) is not None
    said = " ".join(str(s) for s in nav.sent)
    assert "НЕ удалён" in said and "телефон" in said
