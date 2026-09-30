"""E2E: главная администратора — шапка строками по условиям, восемь кнопок,
ссылки шапки и уведомлений (/start online|expiring|unassigned|upd|cl-|dev-|
gw-|extend-|traffic…), «⬆️ Доступна vX» из периодической проверки, списки
«Онлайн» и «Истекают».

Цена ошибки: строка «Истекают» или «Без профиля» пропала при непустом
списке — админ не узнает о чужом пире и о людях без доступа; строка висит при
пустом — шум; ссылка из уведомления не открывает экран — кнопок на
уведомлении больше нет, путь к объекту — только ссылка.
"""
import datetime
from types import SimpleNamespace

import pytest
from aiogram.filters import CommandObject

from awgbot.bot import texts
from awgbot.bot.callbacks import ClientCB, Menu
from awgbot.bot.handlers import admin as ah
from awgbot.bot.handlers.admin.panel import parse_link
from awgbot.core import config
from awgbot.util import timeutil
from tests.conftest import FakeCallback, FakeMessage, FakeState

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID
BOT = "awg_test_bot"


def _cmd(args):
    return CommandObject(prefix="/", command="start", args=args)


def _link(payload, label):
    return f'<a href="https://t.me/{BOT}?start={payload}">{label}</a>'


async def _home(services, bot):
    """Главная по /start: (строки шапки, ряды подписей кнопок)."""
    services.bot_username = BOT
    services.ensure_admin_client()
    msg = FakeMessage(text="/start", chat_id=ADMIN, user_id=ADMIN, bot=bot)
    await ah.admin_start(msg, services, FakeState())
    text, markup = [(t, m) for kind, t, m in msg.sent if kind == "answer"][-1]
    return text.split("\n"), [[b.text for b in r] for r in markup.inline_keyboard]


def _expiring(services, make_active_client, name, tg_id, days=2):
    c = make_active_client(name, tg_id=tg_id)
    now = timeutil.now()
    services.db.update_client_fields(c.id, period_start=timeutil.to_iso(now - datetime.timedelta(days=20)),
                                     period_end=timeutil.to_iso(now + datetime.timedelta(days=days)),
                                     period_kind="month")
    return services.db.get_client(c.id)


# ── шапка ────────────────────────────────────────────────────────────────────

async def test_quiet_home_has_only_what_is_always_there(services, fake_bot, fake_routing):
    """Нечего сказать — нет строк «Истекают», «Без профиля», «Доступна»,
    «Переезд»; кнопок восемь, «🛰 Шлюзы» на месте и без шлюзов: через неё
    РФ-доступ разворачивают и включают."""
    fake_routing.enabled = False
    services.db.set_state("online_count", "0")
    lines, rows = await _home(services, fake_bot)
    assert lines[0].startswith("🛠 <b>") and "🔴 не отвечает" not in lines[0], lines
    # нули — без ссылок: за ними пустые экраны
    assert lines[-2] == "📶 Онлайн: 0", lines
    assert lines[-1] == f"📊 Трафик за {texts.month_label()}: 0 ГБ", lines
    joined = "\n".join(lines)
    assert "<a " not in joined, f"ссылка на пустой экран:\n{joined}"
    assert "РФ-доступ" not in joined, f"строка РФ-доступа без шлюзов и без функции:\n{joined}"
    assert "" not in lines, "пустая строка-разделитель без временных строк"
    for word in ("Истекают", "Без профиля", "Доступна", "Переезд"):
        assert word not in joined, f"строка «{word}» без повода:\n{joined}"
    assert rows == [["📱 Мои устройства"], ["👥 Профили", "➕ Профиль"], ["🛰 Шлюзы", "⚙️ Настройки"],
                    ["📢 Объявление", "🔄 Обновить"]], rows


async def test_home_counters_line_links_expiring_and_unassigned(services, fake_bot, make_active_client):
    """«📶 Онлайн · ⏳ Истекают · 📦 Без профиля» — одной строкой ссылками на
    свои экраны; «Истекают» и «Без профиля» — только при ненулевом числе."""
    _expiring(services, make_active_client, "Скоро", 3101)
    svc = services.db.get_service_client_id()
    services.db.create_device(svc, "чужой", "PUBU", "PSK", "10.8.0.70")
    services.db.set_state("online_count", "7")
    lines, _ = await _home(services, fake_bot)
    assert (" · ".join([_link("online", "📶 Онлайн: 7"), _link("expiring", "⏳ Истекают: 1"),
                        _link("unassigned", "📦 Без профиля: 1")])) in lines, lines


async def test_home_shows_available_update_from_the_periodic_check(services, fake_bot, monkeypatch):
    """«⬆️ Доступна vX» — из ключа, который пишет периодическая проверка
    (update_scan), а не из тега уведомления: он пишется и при выключенных
    уведомлениях. Нечего ставить — строка уходит."""
    monkeypatch.setattr(services, "update_next", lambda: SimpleNamespace(tag="v3.2.0"))
    monkeypatch.setattr(services, "updates_muted", lambda: True)
    assert services.update_scan().tag == "v3.2.0"
    assert services.update_to_notify() is None, "уведомления выключены — уведомлять нечего"
    assert services.update_available_tag() == "v3.2.0", "проверка не записала найденную версию"
    lines, _ = await _home(services, fake_bot)
    upd = ("<b>" + _link("upd", "⬆️ Доступна v3.2.0") + "</b> — "
           '<a href="https://github.com/justSunny12/awg-bot/releases/tag/v3.2.0">список изменений</a>')
    assert lines[-2:] == ["", upd], lines

    monkeypatch.setattr(services, "update_next", lambda: None)
    services.update_scan()
    lines, _ = await _home(services, fake_bot)
    assert not any("Доступна" in ln for ln in lines), "обновились — строка должна уйти"


def test_update_tag_without_v_gets_it_and_migration_line_goes_last():
    """Тег без «v» — с ней, и в подписи, и в адресе страницы релиза;
    временные строки — отдельным блоком через пустую строку; «🚚 Переезд» —
    последней, ссылкой на обзор и только пока идёт переезд."""
    mig = SimpleNamespace(clients_total=12, clients_done=11, devices_total=20, devices_done=18)
    out = texts.admin_panel({"ok": True}, update_tag="3.2.0", migration=mig).split("\n")
    rel = '<a href="https://github.com/justSunny12/awg-bot/releases/tag/v3.2.0">список изменений</a>'
    assert out[-3:] == ["", f"<b>⬆️ Доступна v3.2.0</b> — {rel}",
                        "🚚 Переезд: 11/12 профилей, 18/20 устройств"], out
    out = texts.admin_panel({"ok": True}, update_tag="v3.2.0", migration=mig,
                            bot_username=BOT).split("\n")
    assert out[-3:] == ["", f"<b>{_link('upd', '⬆️ Доступна v3.2.0')}</b> — {rel}",
                        f"{_link('migration', '🚚 Переезд')}: 11/12 профилей, 18/20 устройств"], out
    # один переезд, без обновления — тоже отдельным блоком
    out = texts.admin_panel({"ok": True}, migration=mig).split("\n")
    assert out[-2:] == ["", "🚚 Переезд: 11/12 профилей, 18/20 устройств"], out
    done = SimpleNamespace(clients_total=0, clients_done=0, devices_total=0, devices_done=0)
    assert "Переезд" not in texts.admin_panel({"ok": True}, migration=done)
    assert texts.release_url("v3.2.0") == "https://github.com/justSunny12/awg-bot/releases/tag/v3.2.0"


def test_home_routing_line_is_silent_without_gateways():
    """«🇷🇺 РФ-доступ: 🔴 недоступен» без единого шлюза — шум: сказать нечего,
    добавить шлюз можно в «🛰 Шлюзы». С шлюзом, который не отвечает, — строка
    нужна: иначе админ не узнает, что РФ-доступ лёг."""
    assert texts.routing_admin_status_line({"ok": False, "active": "", "standby": []}) == ""
    down = texts.routing_admin_status_line({"ok": False, "active": "NASPi", "active_slot": 1,
                                            "standby": []})
    assert down.startswith("🇷🇺 РФ-доступ: 🔴 недоступен") and "NASPi" in down, down
    out = texts.admin_panel({"ok": True}, routing_info={"ok": False, "active": "", "standby": []})
    assert "РФ-доступ" not in out, out


def test_home_first_line_is_host_status_and_short_uptime():
    out = texts.admin_panel({"ok": True, "uptime": "12 дней 4 часа"})
    assert out.split("\n")[0].endswith(" · 🟢 работает 12 дн 4 ч"), out
    assert texts.admin_panel({"ok": False}).split("\n")[0].endswith(" · 🔴 не отвечает")


@pytest.mark.parametrize("gateways, enabled", [(False, False), (False, True), (True, False)])
async def test_gateways_button_is_always_there(services, fake_bot, fake_routing, make_active_client,
                                               gateways, enabled):
    """«🛰 Шлюзы» — всегда, рядом с «⚙️ Настройки»: без шлюзов и при
    выключенной функции это единственный вход к её развёртыванию и
    включению."""
    fake_routing.enabled = enabled
    if gateways:
        services.ensure_admin_client()
        pi = services.add_device(services.admin_client().id, "NASPi")
        services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30")
    _, rows = await _home(services, fake_bot)
    assert ["🛰 Шлюзы", "⚙️ Настройки"] in rows, rows
    assert sum(len(r) for r in rows) <= 8 and all(len(r) <= 2 for r in rows), rows


# ── ссылки ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload, link", [
    ("online", ("online", 0)), ("expiring", ("expiring", 0)), ("unassigned", ("unassigned", 0)),
    ("upd", ("upd", 0)), ("traffic", ("traffic", 0)), ("traffic_local", ("traffic", 0)),
    ("traffic-7", ("traffic_dev", 7)), ("traffic_local-7", ("traffic_dev", 7)),
    ("traffic_local-7-t", ("traffic_dev", 7)), ("cl-12", ("cl", 12)), ("dev-5", ("dev", 5)),
    ("gw-2", ("gw", 2)), ("extend-3", ("extend", 3)),
    ("cl-", None), ("cl-abc", None), ("dev-5-x", None), ("Cabcdefghijk", None), ("", None),
])
def test_parse_link_maps_payloads_to_screens(payload, link):
    """Разбор ссылки шапки и уведомлений: известные виды — на свои экраны;
    мусор и коды приглашений (12 знаков с C/F) — не ссылка."""
    assert parse_link(payload) == link


async def _open(services, bot, payload):
    services.bot_username = BOT
    services.db.set_nav_message_id(ADMIN, None)
    msg = FakeMessage(text=f"/start {payload}", chat_id=ADMIN, user_id=ADMIN, bot=bot)
    await ah.admin_start(msg, services, FakeState(), command=_cmd(payload))
    shown = [(t, m) for kind, t, m in msg.sent if kind == "answer"]
    assert shown, f"по ссылке {payload} ничего не пришло"
    text, markup = shown[-1]
    return msg, text, [b.text for r in markup.inline_keyboard for b in r]


async def test_links_open_profile_and_device_cards(services, fake_bot, make_active_client):
    """«cl-<id>» из имени профиля и «dev-<id>» из имени устройства открывают
    карточки; команда убирается из чата; удалённый объект — главная."""
    c = make_active_client("Ксюша", tg_id=3201)
    d = services.add_device(c.id, "iPhone")
    msg, text, labels = await _open(services, fake_bot, f"cl-{c.id}")
    assert msg.deleted and text.startswith("👤 ") and "Ксюша" in text.split("\n")[0], text
    assert labels[:2] == ["⏱ Продлить", "✏️ Изменить"], labels
    _, text, labels = await _open(services, fake_bot, f"dev-{d.device_id}")
    assert text.startswith("⚪ <b>iPhone</b> · ") and _link(f"cl-{c.id}", "Ксюша") in text.split("\n")[0], text
    assert labels[:3] == ["🔗 Ссылка", "🔳 QR", "📄 Файл"], labels
    for payload in ("cl-999999", "dev-999999"):
        _, text, _ = await _open(services, fake_bot, payload)
        assert text.startswith("🛠 "), f"{payload}: не главная, а {text[:40]}"


async def test_link_unassigned_lists_foreign_peers(services, fake_bot):
    svc = services.db.get_service_client_id()
    services.db.create_device(svc, "app", "PUBU", "PSK", "10.8.0.70")
    _, text, labels = await _open(services, fake_bot, "unassigned")
    assert text == "📦 <b>Без профиля:</b> 1 — пир создан мимо бота", text
    assert labels == ["app * · 10.8.0.70", "⬅️ В меню"], "«*» — пир добавлен не ботом"


async def test_link_gw_opens_the_slot_card_and_a_missing_slot_says_so(services, fake_bot, monkeypatch):
    monkeypatch.setattr(services, "gateway_ping", lambda slot: None)
    monkeypatch.setattr(services, "_probe_slot", lambda g, active=False: "down")
    services.ensure_admin_client()
    pi = services.add_device(services.admin_client().id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    _, text, labels = await _open(services, fake_bot, "gw-1")
    head = text.split("\n", 1)[0]
    assert "NASPi</b> — " in head and "Активен" in head, text
    assert "✏️ Изменить" in labels and labels[-1] == "⬅️ Назад", labels
    _, text, _ = await _open(services, fake_bot, "gw-9")
    assert text.startswith("🛰 Такого шлюза больше нет"), text


async def test_link_extend_opens_extension_with_cancel_back_to_expiring(services, fake_bot, make_active_client):
    """«extend-<id>» (из списка истекающих прежнего образца): экран продления;
    после продления — список истекающих, пока в нём кто-то есть."""
    a = _expiring(services, make_active_client, "Аня", 3301)
    _expiring(services, make_active_client, "Боря", 3302, days=3)
    services.bot_username = BOT
    services.db.set_nav_message_id(ADMIN, None)
    state = FakeState()
    msg = FakeMessage(text=f"/start extend-{a.id}", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await ah.admin_start(msg, services, state, command=_cmd(f"extend-{a.id}"))
    text = [t for kind, t, _ in msg.sent if kind == "answer"][-1]
    assert text.startswith(f"⏱ <b>Продление:</b> {_link(f'cl-{a.id}', 'Аня')}"), text
    assert (await state.get_data()).get("return_to") == "expiring"


# ── «Онлайн» и «Истекают» ────────────────────────────────────────────────────

async def test_online_list_puts_gateways_first_with_links_and_blank_lines(
        services, fake_bot, make_active_client, monkeypatch):
    """«📶 Онлайн: N»; шлюзы вверху с «[шлюз]»; «🟢 устройство · профиль ·
    адрес» — имена ссылками на карточки; между записями — пустая строка."""
    c = make_active_client("Ксюша", tg_id=3401)
    phone = services.add_device(c.id, "iPhone")
    services.ensure_admin_client()
    pi = services.add_device(services.admin_client().id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30")
    now = int(timeutil.now().timestamp())
    for dev_id in (phone.device_id, pi.device_id):
        services.db.update_device_fields(dev_id, last_handshake=now)
    _, text, labels = await _open(services, fake_bot, "online")
    ph, gw = services.db.get_device(phone.device_id), services.db.get_device(pi.device_id)
    assert text.split("\n\n") == [
        "📶 <b>Онлайн:</b> 2",
        f"🛰 {_link(f'dev-{gw.id}', 'NASPi')} [шлюз] · <code>{gw.address}</code>",
        f"🟢 {_link(f'dev-{ph.id}', 'iPhone')} · {_link(f'cl-{c.id}', 'Ксюша')} · <code>{ph.address}</code>",
    ], text
    assert labels == ["⬅️ В меню"]


async def test_online_list_when_nobody_is_connected(services, fake_bot):
    services.ensure_admin_client()
    _, text, _ = await _open(services, fake_bot, "online")
    assert text == "📶 <b>Онлайн:</b> 0\n\nСейчас никто не подключён", text


async def test_expiring_list_links_names_and_offers_extend_buttons(services, fake_bot, make_active_client):
    """«⏳ Истекают: 2»; «[Имя] — 3 дн., до 27.09 18:00», ближайшие сверху;
    кнопки «⏱ Имя» — продление прямо из списка."""
    b = _expiring(services, make_active_client, "Боря", 3502, days=3)
    a = _expiring(services, make_active_client, "Аня", 3501, days=1)
    cb = FakeCallback(message=FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=fake_bot),
                      user_id=ADMIN, bot=fake_bot)
    services.bot_username = BOT
    await ah.admin_expiring(cb, services, FakeState())
    text, markup = [(s[1], s[2]) for s in cb.message.sent if s[0] == "edit_text"][-1]
    blocks = text.split("\n\n")
    assert blocks[0] == "⏳ <b>Истекают:</b> 2", text
    for block, c in zip(blocks[1:], (a, b)):
        end = timeutil.parse_iso(c.period_end)
        assert block == (f"{_link(f'cl-{c.id}', c.name)} — {timeutil.remaining_brief(end)}, "
                         f"до {timeutil.fmt_dt_ui(end)}"), block
    btns = [x for r in markup.inline_keyboard for x in r]
    assert [(x.text, x.callback_data) for x in btns] == [
        ("⏱ Аня", ClientCB(action="extend_exp", client_id=a.id).pack()),
        ("⏱ Боря", ClientCB(action="extend_exp", client_id=b.id).pack()),
        ("⬅️ В меню", Menu(action="main").pack())]
