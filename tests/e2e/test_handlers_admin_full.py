"""E2E: роутер администратора (handlers/admin/) — панель, создание/правка/
удаление клиента, устройства клиента, перепривязка устройства без профиля,
бэкап, перезапуск, личный VPN админа.

Не дублирует уже покрытое в test_handlers_block / test_handlers_extend.
"""
import pytest

from awgbot.bot import texts
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


# ── РФ-доступ в карточках, списках и на экранах РФ (концепт «учёт РФ-трафика», этап 2) ──
# Одно правило показа на все места: профилю строка положена, если ему разрешён
# РФ-доступ или за месяц уже что-то прошло; устройству — то же по владельцу,
# кроме шлюза. Расхождение между местами — админ видит РФ в карточке и не
# находит его в списке (или наоборот).

GB = 1024 ** 3
_RF_PREFIX = "└ 🇷🇺 РФ-доступ: "


def _cmd(args):
    from aiogram.filters import CommandObject
    return CommandObject(prefix="/", command="start", args=args)


def _rf_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith(_RF_PREFIX)]


def _entry(text: str, marker: str) -> str:
    """Запись списка (между пустыми строками), где встречается marker."""
    hits = [b for b in text.split("\n\n") if marker in b]
    assert hits, f"{marker!r} нет в списке:\n{text}"
    return hits[0]


def _rf_feature(monkeypatch, services, fake_routing, enabled: bool) -> None:
    """Функция РФ-доступа: обвязка развёрнута (интерфейс линка в app.yaml —
    в тестовой копии он пуст) и выключатель — по enabled. Условие строк РФ —
    ровно это (rf_enabled)."""
    monkeypatch.setattr(services, "routing_provisioned", lambda: True)
    fake_routing.enabled = enabled


def _profile(services, make_active_client, name, tg_id, *, allowed, rf=(0, 0)):
    c = make_active_client(name, tg_id=tg_id)
    if allowed:
        services.db.update_client_fields(c.id, routing_allowed=1)
    dc = services.add_device(c.id, "Телефон")
    if any(rf):
        services.db.rf_add_bulk([(dc.device_id, *rf)])
    return services.db.get_client(c.id), dc.device_id


async def _deep(services, bot, payload: str) -> str:
    """Экран по ссылке /start <payload>: первый раз приходит новым сообщением,
    дальше — правкой живого меню."""
    seen = len(bot.records)
    msg = _amsg(bot, f"/start {payload}")
    await ah.admin_start(msg, services, FakeState(), command=_cmd(payload))
    shown = [t for kind, t, _ in msg.sent if kind == "answer"]
    shown += [r[2] for r in bot.records[seen:] if r[0] == "edit_message_text"]
    assert shown, f"экран по ссылке {payload} не отрисован"
    return shown[-1]


async def _client_card(services, bot, client_id: int) -> str:
    cb, nav = _acb(bot)
    await ah.client_open(cb, ClientCB(action="open", client_id=client_id), services)
    return last_screen(nav)[0]


async def _device_card(services, bot, device_id: int) -> str:
    cb, nav = _acb(bot)
    await ah.admin_device_open(cb, DeviceCB(action="open", device_id=device_id), services)
    return last_screen(nav)[0]


@pytest.mark.parametrize("enabled, allowed, rf, expect", [
    (True, True, (0, 0), "0 ГБ (↑ 0 ГБ | ↓ 0 ГБ)"),                 # разрешён и 0 → строка
    (False, True, (0, 0), None),                                      # разрешён, 0, функция выключена → нет
    (True, False, (0, 0), None),                                      # не разрешён и 0 → нет
    (True, False, (GB, 3 * GB), "4 ГБ (↑ 1 ГБ | ↓ 3 ГБ)"),            # не разрешён, но было → строка
    (False, True, (GB, 3 * GB), "4 ГБ (↑ 1 ГБ | ↓ 3 ГБ)"),            # выключили, байты остались → строка
])
async def test_profile_rf_line_follows_one_rule_in_card_list_and_rf_screen(
        services, fake_bot, fake_routing, make_active_client, monkeypatch, enabled, allowed, rf, expect):
    """Профиль: строка РФ в карточке (сразу под потреблением профиля), под его
    строкой в списке потребления и строкой на экране РФ — везде по одному
    правилу. Не разрешён (или функция на сервере не развёрнута/выключена) и
    ноль — строки нет нигде: иначе у профилей висит «РФ-доступ: 0 ГБ»,
    которого у них быть не может."""
    _rf_feature(monkeypatch, services, fake_routing, enabled)
    c, _ = _profile(services, make_active_client, "Ксюша", 6301, allowed=allowed, rf=rf)

    card = await _client_card(services, fake_bot, c.id)
    lines = card.splitlines()
    if expect is None:
        assert _rf_lines(card) == [], card
    else:
        assert _rf_lines(card) == [_RF_PREFIX + expect], card
        head = next(i for i, ln in enumerate(lines) if ln.startswith("Потребление профиля за месяц"))
        assert lines[head + 1] == _RF_PREFIX + expect, "строка РФ не сразу под потреблением профиля"

    entry = _entry(await _deep(services, fake_bot, "traffic"), "Ксюша")
    assert _rf_lines(entry) == ([] if expect is None else [_RF_PREFIX + expect]), entry

    screen = await _deep(services, fake_bot, "traffic_local")
    if expect is None:
        assert "Ксюша" not in screen, screen
    else:
        assert f"👤 Ксюша: {expect}" in screen, screen


@pytest.mark.parametrize("enabled, allowed, rf, expect", [
    (True, True, (0, 0), "0 ГБ (↑ 0 ГБ | ↓ 0 ГБ)"),
    (False, True, (0, 0), None),
    (True, False, (0, 0), None),
    (True, False, (GB, GB), "2 ГБ (↑ 1 ГБ | ↓ 1 ГБ)"),
    (False, True, (GB, GB), "2 ГБ (↑ 1 ГБ | ↓ 1 ГБ)"),
])
async def test_device_rf_line_follows_one_rule_in_card_list_and_rf_screen(
        services, fake_bot, fake_routing, make_active_client, monkeypatch, enabled, allowed, rf, expect):
    """Устройство: карточка (под «Потребление»), разбивка потребления профиля
    и экран РФ профиля — по одному правилу от владельца, состояния функции и
    собственных байт."""
    _rf_feature(monkeypatch, services, fake_routing, enabled)
    c, did = _profile(services, make_active_client, "Ксюша", 6302, allowed=allowed, rf=rf)

    card = await _device_card(services, fake_bot, did)
    lines = card.splitlines()
    if expect is None:
        assert _rf_lines(card) == [], card
    else:
        assert _rf_lines(card) == [_RF_PREFIX + expect], card
        head = next(i for i, ln in enumerate(lines) if ln.startswith("Потребление:"))
        assert lines[head + 1] == _RF_PREFIX + expect, "строка РФ не сразу под потреблением"

    entry = _entry(await _deep(services, fake_bot, f"traffic-{c.id}"), "Телефон")
    assert _rf_lines(entry) == ([] if expect is None else [_RF_PREFIX + expect]), entry

    screen = await _deep(services, fake_bot, f"traffic_local-{c.id}")
    if expect is None:
        assert "Телефон" not in screen and "Устройств с РФ-доступом нет." in screen, screen
    else:
        assert f"🔴 Телефон: {expect}" in screen, screen


@pytest.fixture()
def rf_gateway(services, make_active_client, monkeypatch):
    """Профиль админа с телефоном и устройством-шлюзом; у обоих РФ-счётчики
    (у шлюза — как если бы байты на него всё-таки легли)."""
    monkeypatch.setattr(services, "gateway_ping", lambda slot: None)
    monkeypatch.setattr(services, "_probe_slot", lambda g, active=False: "down")
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    phone = services.add_device(admin.id, "phone")
    pi = services.add_device(admin.id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.db.rf_add_bulk([(phone.device_id, GB, GB), (pi.device_id, 5 * GB, 5 * GB)])
    return admin, phone.device_id, pi.device_id


async def test_gateway_device_gets_no_rf_line_anywhere(services, fake_bot, rf_gateway):
    """Шлюз — не потребитель: его строки РФ нет ни в карточке, ни в
    разбивке, ни на экране РФ профиля, даже если байты на нём есть."""
    admin, phone_id, pi_id = rf_gateway
    card = await _device_card(services, fake_bot, pi_id)
    assert "🛰" in card and _rf_lines(card) == [], card
    breakdown = await _deep(services, fake_bot, f"traffic-{admin.id}")
    assert _rf_lines(_entry(breakdown, "NASPi")) == [], breakdown
    assert _rf_lines(_entry(breakdown, "phone")) == [_RF_PREFIX + "2 ГБ (↑ 1 ГБ | ↓ 1 ГБ)"], breakdown
    screen = await _deep(services, fake_bot, f"traffic_local-{admin.id}")
    assert "NASPi" not in screen and "phone: 2 ГБ (↑ 1 ГБ | ↓ 1 ГБ)" in screen, screen


async def test_profile_rf_sum_leaves_gateway_devices_out(services, fake_bot, rf_gateway):
    """РФ профиля — сумма его устройств без шлюзов: карточка, список
    потребления и экран РФ показывают 2 ГБ телефона, а не 12 ГБ со шлюзом."""
    admin, _, _ = rf_gateway
    want = _RF_PREFIX + "2 ГБ (↑ 1 ГБ | ↓ 1 ГБ)"
    assert _rf_lines(await _client_card(services, fake_bot, admin.id)) == [want]
    assert _rf_lines(_entry(await _deep(services, fake_bot, "traffic"), "Админ")) == [want]
    assert "👤 Админ: 2 ГБ (↑ 1 ГБ | ↓ 1 ГБ)" in await _deep(services, fake_bot, "traffic_local")


async def test_rf_screen_lists_admin_first_and_is_empty_without_profiles(
        services, fake_bot, fake_routing, make_active_client, monkeypatch):
    """Пусто — своя фраза, а не голый заголовок; дальше админ первым, как в
    списке клиентов, — ему разрешено всегда (функция развёрнута и включена)."""
    _rf_feature(monkeypatch, services, fake_routing, True)
    empty = await _deep(services, fake_bot, "traffic_local")
    assert empty.endswith("\n\nПрофилей с РФ-доступом нет."), empty
    _profile(services, make_active_client, "Алёна", 6303, allowed=True)
    services.ensure_admin_client()
    screen = await _deep(services, fake_bot, "traffic_local")
    rows = [b for b in screen.split("\n\n") if b.startswith("👤 ")]
    assert len(rows) == 2 and "Алёна" in rows[1], screen
    assert "Профилей с РФ-доступом нет." not in screen


async def test_rf_lines_stay_when_the_routing_self_check_fails(
        services, fake_bot, fake_routing, make_active_client, monkeypatch):
    """Самопроверка обвязки отрицательная (ipset/dnsmasq сломались), функция
    развёрнута и включена: строки РФ «0 ГБ» у профиля и устройства остаются —
    как строка на главной. Раньше они пропадали, и админ видел «РФ-доступа
    нет» ровно тогда, когда нулевой счётчик и есть главная подсказка."""
    from awgbot.infra import routing as infra_routing
    _rf_feature(monkeypatch, services, fake_routing, True)
    monkeypatch.setattr(infra_routing, "available", lambda: False)
    assert not services.routing_available(), "подмена самопроверки не сработала"
    c, did = _profile(services, make_active_client, "Ксюша", 6420, allowed=True)
    want = [_RF_PREFIX + "0 ГБ (↑ 0 ГБ | ↓ 0 ГБ)"]
    assert _rf_lines(await _client_card(services, fake_bot, c.id)) == want, "строка профиля пропала"
    assert _rf_lines(await _device_card(services, fake_bot, did)) == want, "строка устройства пропала"
    assert "👤 Ксюша: 0 ГБ" in await _deep(services, fake_bot, "traffic_local")


async def test_rf_lines_need_the_feature_deployed_not_only_switched_on(
        services, fake_bot, fake_routing, make_active_client, monkeypatch):
    """Выключатель включён, а обвязка не развёрнута (интерфейса линка нет):
    функции нет — нулевых строк РФ тоже нет."""
    fake_routing.enabled = True
    monkeypatch.setattr(services, "routing_provisioned", lambda: False)
    c, did = _profile(services, make_active_client, "Ксюша", 6421, allowed=True)
    assert _rf_lines(await _client_card(services, fake_bot, c.id)) == []
    assert _rf_lines(await _device_card(services, fake_bot, did)) == []


async def test_admin_gets_no_zero_rf_line_on_a_server_without_the_feature(
        services, fake_bot, fake_routing):
    """Админу РФ-доступ разрешён всегда; но на сервере без шлюзов (функция не
    развёрнута или выключена) вечное «РФ-доступ: 0 ГБ» в его карточке, в
    потреблении и на экране РФ — шум про то, чего нет."""
    fake_routing.enabled = False
    services.ensure_admin_client()
    admin = services.admin_client()
    services.add_device(admin.id, "phone")
    assert _rf_lines(await _client_card(services, fake_bot, admin.id)) == []
    assert _rf_lines(await _deep(services, fake_bot, "traffic")) == []
    assert _rf_lines(await _deep(services, fake_bot, f"traffic-{admin.id}")) == []
    screen = await _deep(services, fake_bot, "traffic_local")
    assert "👤 " not in screen and "Профилей с РФ-доступом нет." in screen, screen


def _rf_total(services, rx: int, tx: int, since: str = ""):
    services.db.set_state("rf_month_rx", str(rx))
    services.db.set_state("rf_month_tx", str(tx))
    services.db.set_state("rf_acct_since", since)


_OUTSIDE = "🧐 <b>Вне профилей:</b> "


async def test_rf_screen_shows_outside_as_total_minus_devices(services, fake_bot, make_active_client):
    """«Вне профилей» — итог сервера минус сумма по устройствам: без неё сумма
    строк не сходится с заголовком, и админ ищет ошибку в счёте."""
    _profile(services, make_active_client, "Ксюша", 6304, allowed=True, rf=(GB, GB))
    extra = int(0.22 * GB)
    _rf_total(services, GB, GB + extra)
    screen = await _deep(services, fake_bot, "traffic_local")
    assert screen.startswith(f"🇷🇺 <b>РФ-доступ за {texts.month_label()}:</b>\n2.22 ГБ"), screen
    assert screen.endswith("\n\n🧐 <b>Вне профилей:</b> 0.22 ГБ — удалённые устройства и первые минуты новых."), \
        screen


@pytest.mark.parametrize("extra, shown", [
    (0, False),                      # сошлось — строки нет
    (GB // 100 - 1, False),          # меньше 0.01 ГБ — шум округления, не показываем
    (GB // 100, True),               # ровно 0.01 ГБ — уже видно
])
async def test_rf_screen_outside_threshold(services, fake_bot, make_active_client, extra, shown):
    _profile(services, make_active_client, "Ксюша", 6305, allowed=True, rf=(GB, GB))
    _rf_total(services, GB, GB + extra)
    screen = await _deep(services, fake_bot, "traffic_local")
    assert (_OUTSIDE in screen) is shown, screen
    if shown:
        assert _OUTSIDE + "0.01 ГБ —" in screen, screen


async def test_rf_screen_hides_outside_when_devices_exceed_total(services, fake_bot, make_active_client):
    """Устройства насчитали больше итога (итог сбросили, строки — ещё нет):
    «вне профилей» не уходит в минус и не показывается."""
    _profile(services, make_active_client, "Ксюша", 6306, allowed=True, rf=(3 * GB, 3 * GB))
    _rf_total(services, GB, GB)
    screen = await _deep(services, fake_bot, "traffic_local")
    assert _OUTSIDE not in screen, screen


async def test_deleted_device_moves_its_rf_into_outside(services, fake_bot, make_active_client):
    """Устройство удалили в середине месяца: его РФ остаётся в итоге сервера и
    переезжает в «вне профилей», заголовок не меняется."""
    c = make_active_client("Ксюша", tg_id=6307)
    services.db.update_client_fields(c.id, routing_allowed=1)
    keep = services.add_device(c.id, "Телефон")
    gone = services.add_device(c.id, "Ноут")
    services.db.rf_add_bulk([(keep.device_id, GB, GB), (gone.device_id, GB, GB)])
    _rf_total(services, 2 * GB, 2 * GB)
    before = await _deep(services, fake_bot, "traffic_local")
    assert _OUTSIDE not in before and "👤 Ксюша: 4 ГБ" in before, before
    services.remove_device(gone.device_id)
    after = await _deep(services, fake_bot, "traffic_local")
    assert after.startswith(f"🇷🇺 <b>РФ-доступ за {texts.month_label()}:</b>\n4 ГБ"), after
    assert "👤 Ксюша: 2 ГБ" in after, after
    assert _OUTSIDE + "2 ГБ —" in after, after


async def test_rf_screen_has_no_start_date_even_in_the_start_month(services, fake_bot):
    """Строку «Учёт — с …» сняли при вычитке 3.1.0: заголовок — месяц и итог,
    вторая строка — сразу цифры. Вернётся дата — экран снова разойдётся с
    экраном трафика по виду шапки."""
    from awgbot.util import timeutil
    now = timeutil.now()
    _rf_total(services, 0, 0, since=timeutil.to_iso(now))
    lines = (await _deep(services, fake_bot, "traffic_local")).split("\n")
    assert lines[:2] == [f"🇷🇺 <b>РФ-доступ за {texts.month_label()}:</b>",
                         "0 ГБ (↑ 0 ГБ | ↓ 0 ГБ)"], lines
    assert not any(ln.startswith("Учёт — с") for ln in lines), lines


async def test_rf_screen_survives_garbage_in_start_date(services, fake_bot):
    """Битая отметка начала учёта в state — экран без даты, а не исключение
    в хендлере (админ жмёт ссылку — и ничего)."""
    _rf_total(services, GB, 0, since="не дата")
    screen = await _deep(services, fake_bot, "traffic_local")
    assert screen.startswith(f"🇷🇺 <b>РФ-доступ за {texts.month_label()}:</b>\n1 ГБ") \
        and "Учёт — с" not in screen, screen


# ── шапки и порядок экранов трафика и РФ (вычитка 3.1.0, часть VII) ──────────

def _profile_rows(screen: str) -> list[str]:
    """Имена профилей на экране по порядку (строки «👤 Имя: …»)."""
    return [b.split("\n")[0][2:].split(":")[0] for b in screen.split("\n\n") if b.startswith("👤 ")]


async def test_traffic_screen_heads_with_the_server_total_and_puts_the_biggest_first(
        services, fake_bot, make_active_client):
    """Шапка «Трафик за ММ.ГГГГ» и под ней итог сервера за месяц; профили — от
    большего к меньшему, равные — в прежнем порядке. Итог не тот — сумма строк
    не сходится с главной; порядок по созданию — крупный потребитель теряется
    внизу длинного списка."""
    a, da = _profile(services, make_active_client, "Алёна", 6401, allowed=False)
    b, db_ = _profile(services, make_active_client, "Борис", 6402, allowed=False)
    _profile(services, make_active_client, "Вера", 6403, allowed=False)
    _profile(services, make_active_client, "Глеб", 6404, allowed=False)
    services.db.add_traffic_bulk([(da, GB, 0), (db_, GB, 2 * GB)])
    screen = await _deep(services, fake_bot, "traffic")
    lines = screen.split("\n")
    assert lines[:2] == [f"📊 <b>Трафик за {texts.month_label()}:</b>", "4 ГБ (↑ 2 ГБ | ↓ 2 ГБ)"], lines
    assert _profile_rows(screen) == ["Борис", "Алёна", "Вера", "Глеб"], screen


async def test_traffic_devices_screen_heads_with_the_profile_total(
        services, fake_bot, make_active_client):
    """Разбивка профиля: «Трафик за ММ.ГГГГ, Имя:» и под ней итог профиля, а не
    сервера — иначе цифра в шапке не совпадёт со строкой профиля в списке."""
    c, did = _profile(services, make_active_client, "Ксюша", 6405, allowed=False)
    other, oid = _profile(services, make_active_client, "Чужой", 6406, allowed=False)
    services.db.add_traffic_bulk([(did, GB, 2 * GB), (oid, 5 * GB, 5 * GB)])
    lines = (await _deep(services, fake_bot, f"traffic-{c.id}")).split("\n")
    assert lines[:2] == [f"📊 <b>Трафик за {texts.month_label()}, Ксюша:</b>",
                         "3 ГБ (↑ 1 ГБ | ↓ 2 ГБ)"], lines


async def test_rf_screen_puts_the_biggest_profile_first(services, fake_bot, fake_routing,
                                                        make_active_client, monkeypatch):
    """Экран РФ-доступа — тот же порядок, что у трафика: от большего к меньшему,
    равные — в прежнем порядке."""
    _rf_feature(monkeypatch, services, fake_routing, True)
    _profile(services, make_active_client, "Алёна", 6407, allowed=True, rf=(GB, 0))
    _profile(services, make_active_client, "Борис", 6408, allowed=True, rf=(GB, 2 * GB))
    _profile(services, make_active_client, "Вера", 6409, allowed=True)
    _profile(services, make_active_client, "Глеб", 6410, allowed=True)
    _rf_total(services, 2 * GB, 2 * GB)
    screen = await _deep(services, fake_bot, "traffic_local")
    assert _profile_rows(screen) == ["Борис", "Алёна", "Вера", "Глеб"], screen


async def test_rf_devices_screen_heads_with_the_profile_rf(services, fake_bot, make_active_client):
    """Разбивка РФ профиля: «РФ-доступ за ММ.ГГГГ, Имя:» и РФ профиля (сумма
    его устройств), а не итог сервера."""
    c, did = _profile(services, make_active_client, "Ксюша", 6411, allowed=True, rf=(GB, GB))
    _profile(services, make_active_client, "Чужой", 6412, allowed=True, rf=(3 * GB, 3 * GB))
    _rf_total(services, 10 * GB, 10 * GB)
    lines = (await _deep(services, fake_bot, f"traffic_local-{c.id}")).split("\n")
    assert lines[:2] == [f"🇷🇺 <b>РФ-доступ за {texts.month_label()}, Ксюша:</b>",
                         "2 ГБ (↑ 1 ГБ | ↓ 1 ГБ)"], lines
