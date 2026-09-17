"""E2E: добор веток роутера админа — понижение лимита с подтверждением,
продление, выдача файла, блок с приостановкой (FSM дней), личные qr/file,
выбор устройства для добавления, карточка пира без приватного ключа,
deep-links панели (потребление, онлайн, истекающие).

Объявления — test_handlers_broadcast.py; почта и бэкапы из чата —
test_handlers_settings_mail_backup.py; раскладка настроек — test_settings_layout.py.
"""

import pytest

from awgbot.bot.handlers import admin as ah
from awgbot.bot.callbacks import AdminSelfCB, BlockCB, ClientCB, ConfirmCB, DeviceCB
from awgbot.core import config
from tests.conftest import FakeCallback, FakeMessage, FakeState, last_screen

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
    _, labels = last_screen(nav)
    assert any("год" in l.lower() for l in labels), "нет выбора срока"
    assert any("Отмена" in l for l in labels), "диалог выбора срока — не тупик"


async def test_admin_dev_file(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6304)
    dc = services.add_device(client.id, "d")
    cb, nav = _acb(fake_bot)
    await ah.admin_dev_gen(cb, DeviceCB(action="gen_file", device_id=dc.device_id), services)
    assert any(s[0] == "document" for s in nav.sent)


# ── блок клиента с приостановкой (FSM дней) ──────────────────────────────────
async def test_block_client_pause_flow(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6305, period_kind="year")
    cb, nav = _acb(fake_bot)
    await ah.admin_block_menu(cb, BlockCB(target="cli", action="menu_block", ref=client.id))
    assert any(s[0] == "edit_text" for s in nav.sent)      # спросили про приостановку
    st = FakeState()
    cb2, nav2 = _acb(fake_bot)
    await ah.admin_block_pause_yes(cb2, BlockCB(target="cli", action="pause_yes", ref=client.id), st, services)
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
    text, labels = last_screen(nav)
    assert "заблокировать" in text.lower()
    assert any("уведомлением" in l for l in labels) and any("Тихо" in l for l in labels)
    assert any("Отмена" in l for l in labels), "меню блокировки — не тупик"


# ── личные qr/file, выбор устройства ─────────────────────────────────────────
async def test_self_gen_qr_file_pickers(services, fake_bot):
    services.ensure_admin_client()
    ac = services.admin_client()
    services.add_device(ac.id, "d")
    for action in ("gen_qr", "gen_file"):
        cb, nav = _acb(fake_bot)
        await ah.self_gen_pick(cb, AdminSelfCB(action=action), services)
        _, labels = last_screen(nav)
        assert any("d" == l.strip("🖥📱💻 ") or l.endswith(" d") or l == "d" for l in labels) \
            or any("d" in l for l in labels), "устройство не предложено к выбору"


async def test_add_device_choice_and_pick(services, fake_bot, make_active_client):
    make_active_client(tg_id=6307, name="Клиент-6307")
    cb, nav = _acb(fake_bot)
    await ah.admin_add_device_choice(cb, services)
    _, labels = last_screen(nav)
    assert len([l for l in labels if "Назад" not in l]) >= 2, "выбор «кому» без вариантов"
    cb2, nav2 = _acb(fake_bot)
    await ah.admin_add_device_pick(cb2, services)
    _, labels2 = last_screen(nav2)
    assert any("Клиент" in l for l in labels2), "профиль не предложен для добавления устройства"


async def test_admin_menu_devices(services, fake_bot):
    services.ensure_admin_client()
    services.add_device(services.admin_client().id, "Ноут")
    cb, nav = _acb(fake_bot)
    await ah.admin_menu_devices(cb, services)
    text, labels = last_screen(nav)
    assert "Устройств добавлено: 1" in text
    assert any("Ноут" in l for l in labels), "устройство админа не показано"


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


async def test_admin_card_hides_the_reinvite_button(services, make_active_client):
    """Устройство с незавершённым инвайтом другу: у владельца кнопка «Перевыдать
    инвайт» есть, у админа на карточке того же устройства — нет.

    Хендлер перевыдачи живёт в роутере клиента (ссылка уходит в чат владельца);
    для админа нажатие уходило в никуда — спиннер до таймаута.
    """
    from awgbot.bot import keyboards as kb

    client = make_active_client(tg_id=5009)
    did = services.add_device(client.id, "d").device_id
    services.make_device_friendly(did)
    dev = services.db.get_device(did)
    assert dev.friend_status == "pending"

    owner = _btn_texts(kb.device_actions(dev, is_admin=False, back_target="x"))
    admin = _btn_texts(kb.device_actions(dev, is_admin=True, back_target="x",
                                         reassign_label="🔀 Передать"))
    assert any("Перевыдать" in t for t in owner)
    assert not any("Перевыдать" in t for t in admin)


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

def _btn_texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


# ── потребление за месяц: ссылки из панели ───────────────────────────────────

def _cmd(args):
    from aiogram.filters import CommandObject
    return CommandObject(prefix="/", command="start", args=args)


async def test_panel_traffic_line_is_a_deep_link(services, fake_bot):
    from awgbot.bot import texts
    out = texts.admin_panel({"ok": True, "traffic_rx": 1, "traffic_tx": 2},
                            bot_username="awg_test_bot")
    assert 'href="https://t.me/awg_test_bot?start=traffic">📊 Потребление за месяц (все)</a>' in out
    assert "Потребление за месяц (все)</a>: 0.01 ГБ (↑ 0.01 ГБ | ↓ 0.01 ГБ)" in out


async def test_start_traffic_opens_profiles_and_removes_the_command(
        services, make_active_client, fake_bot):
    from awgbot.bot.handlers import admin as ah
    from tests.conftest import FakeMessage, FakeState
    import awgbot.core.config as cfg
    services.bot_username = "awg_test_bot"
    c = make_active_client("Профиль А")
    msg = FakeMessage(text="/start traffic", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await ah.admin_start(msg, services, FakeState(), command=_cmd("traffic"))
    assert any(r[0] == "delete" for r in fake_bot.records), "команда /start traffic не удалена"
    sent = [t for kind, t, _ in msg.sent if kind == "answer"]
    assert sent and "Потребление трафика за текущий месяц" in sent[-1]
    assert f'👤 <a href="https://t.me/awg_test_bot?start=traffic-{c.id}">Профиль А</a>' in sent[-1]


async def test_start_traffic_replaces_the_active_menu_in_place(services, make_active_client, fake_bot):
    """Экран потребления встаёт НА МЕСТО панели (редактированием), а не под ней."""
    from awgbot.bot.handlers import admin as ah
    from tests.conftest import FakeMessage, FakeState
    import awgbot.core.config as cfg
    services.db.set_nav_message_id(cfg.ADMIN_ID, 777)
    msg = FakeMessage(text="/start traffic", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await ah.admin_start(msg, services, FakeState(), command=_cmd("traffic"))
    edits = [r for r in fake_bot.records if r[0] == "edit_message_text"]
    assert edits and "Потребление трафика за текущий месяц" in edits[-1][2]
    assert not any(kind == "answer" for kind, _, _ in msg.sent), "экран ушёл новым сообщением"


async def test_start_traffic_client_opens_devices_and_back_leads_to_profiles(
        services, make_active_client, fake_bot):
    from awgbot.bot.handlers import admin as ah
    from awgbot.bot.callbacks import Menu
    from tests.conftest import FakeCallback, FakeMessage, FakeState
    import awgbot.core.config as cfg
    c = make_active_client("Профиль Б")
    msg = FakeMessage(text=f"/start traffic-{c.id}", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await ah.admin_start(msg, services, FakeState(), command=_cmd(f"traffic-{c.id}"))
    sent = [(t, m) for kind, t, m in msg.sent if kind == "answer"]
    assert sent and "Потребление профиля Профиль Б за текущий месяц:" in sent[-1][0]
    back = [b for row in sent[-1][1].inline_keyboard for b in row]
    assert back and back[0].callback_data == Menu(action="traffic").pack()
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await ah.admin_traffic_profiles(cb, services)
    assert any("Потребление трафика за текущий месяц" in t for kind, t, _ in msg.sent if kind == "edit_text")


async def test_plain_start_still_purges_and_shows_panel(services, fake_bot):
    from awgbot.bot.handlers import admin as ah
    from tests.conftest import FakeMessage, FakeState
    import awgbot.core.config as cfg
    msg = FakeMessage(text="/start", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await ah.admin_start(msg, services, FakeState(), command=_cmd(None))
    assert any("Панель администратора" in t for kind, t, _ in msg.sent if kind == "answer")


async def test_devices_breakdown_lists_real_devices_with_traffic(services, make_active_client, fake_bot):
    """Регресс: с реальными устройствами экран падал на поле трафика (в бою —
    AttributeError, в тесте профиль был без устройств)."""
    from awgbot.bot.handlers import admin as ah
    from tests.conftest import FakeMessage, FakeState
    import awgbot.core.config as cfg
    c = make_active_client("Профиль В")
    services.add_device(c.id, "Телефон")
    dev = services.db.list_devices(c.id)[0]
    services.db.add_traffic_bulk([(dev.id, 3 * 1024 ** 2, 5 * 1024 ** 2)])
    msg = FakeMessage(text=f"/start traffic-{c.id}", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await ah.admin_start(msg, services, FakeState(), command=_cmd(f"traffic-{c.id}"))
    sent = [t for kind, t, _ in msg.sent if kind == "answer"]
    assert sent and "🔴 Телефон: 0.01 ГБ (↑ 0.01 ГБ | ↓ 0.01 ГБ)" in sent[-1]


def test_transfer_buttons_are_split_by_role(services, make_active_client):
    """Владелец: «Передать другу», без «в другой профиль». Админ: наоборот."""
    from awgbot.bot import keyboards as kbs
    c = make_active_client("Профиль Г")
    services.add_device(c.id, "Ноут")
    dev = services.db.list_devices(c.id)[0]
    owner = [b.text for row in kbs.device_actions(dev, is_admin=False, back_target="x").inline_keyboard for b in row]
    admin = [b.text for row in kbs.device_actions(dev, is_admin=True, back_target="x",
                                                  reassign_label="🔀 Передать в другой профиль").inline_keyboard for b in row]
    assert "👤 Передать другу" in owner and "🔀 Передать в другой профиль" not in owner
    assert "🔀 Передать в другой профиль" in admin and "👤 Передать другу" not in admin


# ── онлайн: статус в списке получателей и экран устройств онлайн ─────────────

def test_broadcast_targets_mark_subscription_only_in_extend_mode(services, make_active_client):
    """Онлайн-кружков в выборе адресатов нет (не онлайн — прочитает потом). В
    режиме с продлением справа от имени — состояние подписки: ∞ бессрочная,
    ⛔ и дата — истекла; активной — ничего."""
    from awgbot.bot import keyboards as kbs
    a = make_active_client("Анна", tg_id=1001)
    b = make_active_client("Борис", tg_id=1002, period_kind="never")
    v = make_active_client("Вера", tg_id=1003)
    services.db.update_client_fields(v.id, period_end="2026-09-01T00:00:00+03:00", status="expired")
    rows = [services.db.get_client(c.id) for c in (a, b, v)]
    def labels(extend):
        return [btn.text for row in kbs.broadcast_targets(rows, set(), extend=extend).inline_keyboard
                for btn in row if btn.text.startswith(("✅", "☑️")) and "все" not in btn.text]
    assert labels(False) == ["☑️ Анна", "☑️ Борис", "☑️ Вера"]
    assert labels(True) == ["☑️ Анна", "☑️ Борис ∞", "☑️ Вера ⛔ 01.09.2026"]


async def test_online_link_opens_the_list_of_online_devices(services, make_active_client, fake_bot, monkeypatch):
    from awgbot.bot import texts
    from awgbot.bot.handlers import admin as ah
    from tests.conftest import FakeMessage, FakeState
    from awgbot.util import timeutil
    import awgbot.core.config as cfg
    out = texts.admin_panel({"ok": True, "online_count": 2}, bot_username="awg_test_bot")
    assert 'href="https://t.me/awg_test_bot?start=online">📶 Устройств онлайн</a>: 2' in out
    c = make_active_client("Профиль Д")
    services.add_device(c.id, "iPhone 16 Pro"); services.add_device(c.id, "Старый ноут")
    on, off = services.db.list_devices(c.id)
    monkeypatch.setattr(timeutil, "handshake_is_online", lambda hs: hs == "on")
    monkeypatch.setattr(services, "online_devices",
                        lambda: [(d, "Профиль Д") for d in services.db.list_devices(c.id) if d.id == on.id])
    msg = FakeMessage(text="/start online", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await ah.admin_start(msg, services, FakeState(), command=_cmd("online"))
    sent = [t for kind, t, _ in msg.sent if kind == "answer"]
    assert sent and "📶 <b>Устройства онлайн (1):</b>" in sent[-1]
    assert f"📱 iPhone 16 Pro (Профиль Д) — {texts.plain_ip(on.address)}" in sent[-1] and "Старый ноут" not in sent[-1]
    assert f"<code>{on.address}</code>" in sent[-1], "адрес ушёл голым — Telegram сделает из него ссылку"


def test_device_line_format_and_plain_ip(services, make_active_client):
    from awgbot.bot import texts
    c = make_active_client("Профиль Е")
    services.add_device(c.id, "iPhone 16 Pro")
    dev = services.db.list_devices(c.id)[0]
    line = texts.device_line(dev)
    assert line.startswith(f"🔴 iPhone 16 Pro ({texts.plain_ip(dev.address)}), последний коннект: ")
    assert texts.plain_ip("10.9.1.2") == "<code>10.9.1.2</code>"


def test_list_screens_separate_entries_with_a_blank_line(services, make_active_client):
    from awgbot.bot import texts
    a = make_active_client("А", tg_id=1101); b = make_active_client("Б", tg_id=1102)
    out = texts.traffic_profiles_text([(a, 1, 1), (b, 2, 2)], "bot")
    assert "\n\n👤 " in out and out.count("\n\n") == 2


# ── истекающие подписки: панель, экран, продление с возвратом ────────────────

async def test_expiring_line_is_conditional_and_linked(services, make_active_client, fake_bot):
    from awgbot.bot import texts
    assert "Истекающие" not in texts.admin_panel({"ok": True}, bot_username="b", expiring=0)
    out = texts.admin_panel({"ok": True}, bot_username="b", expiring=2)
    assert out.endswith('<b><a href="https://t.me/b?start=expiring">⏳ Истекающие подписки</a>: 2</b>')


async def test_expiring_screen_and_extend_returns_to_it_or_menu(services, make_active_client, fake_bot):
    import datetime as dt
    from awgbot.bot.handlers import admin as ah
    from awgbot.bot.callbacks import PeriodCB, Menu
    from awgbot.util import timeutil
    from tests.conftest import FakeCallback, FakeMessage, FakeState
    import awgbot.core.config as cfg
    services.bot_username = "b"
    now = timeutil.now()
    c = make_active_client("Скоро", tg_id=3001)
    services.db.update_client_fields(c.id, period_start=timeutil.to_iso(now - dt.timedelta(days=20)),
                                     period_end=timeutil.to_iso(now + dt.timedelta(days=3)), period_kind="month")
    msg = FakeMessage(text="/start expiring", chat_id=cfg.ADMIN_ID, user_id=cfg.ADMIN_ID, bot=fake_bot)
    state = FakeState()
    await ah.admin_start(msg, services, state, command=_cmd("expiring"))
    sent = [t for kind, t, _ in msg.sent if kind == "answer"]
    assert sent and "⏳ <b>Истекающие подписки:</b>" in sent[-1]
    assert "👤 Скоро — осталось 2 дня 23 часа" in sent[-1] and "Период подписки: " in sent[-1]
    assert f'?start=extend-{c.id}">Продлить?</a>' in sent[-1]
    # «Продлить?» → выбор срока с отменой обратно в список (активное меню
    # сбрасываем, чтобы экран ушёл ответом, а не правкой — так видна клавиатура)
    services.db.set_nav_message_id(cfg.ADMIN_ID, None)
    await ah.admin_start(msg, services, state, command=_cmd(f"extend-{c.id}"))
    sent = [(t, m) for kind, t, m in msg.sent if kind == "answer"]
    assert "На какой срок продлить?" in sent[-1][0]
    cancel = [b for row in sent[-1][1].inline_keyboard for b in row if "Отмена" in b.text][0]
    assert cancel.callback_data == Menu(action="expiring").pack()
    assert (await state.get_data())["return_to"] == "expiring"
    # продление на год: остатка нет вопроса? остаток есть → вопрос; отвечаем «нет»
    cb = FakeCallback(message=msg, user_id=cfg.ADMIN_ID, bot=fake_bot)
    await ah.extend_period_chosen(cb, PeriodCB(kind="year", ctx="extend", ref=c.id), services, state)
    from awgbot.bot.callbacks import ConfirmCB
    await ah.extend_keep_answer(cb, ConfirmCB(action="keep", ref=c.id, yes=False), services, state)
    edits = [t for kind, t, _ in msg.sent if kind == "edit_text"]
    assert any(t.startswith("✅ Подписка профиля Скоро продлена на 1 год, до ")
               for t in edits), "итог не остался инфосообщением"
    answers = [t for kind, t, _ in msg.sent if kind == "answer"]
    assert "Панель администратора" in answers[-1], "после продления не вернулись в меню (список опустел)"
    assert "продлена на" not in answers[-1], "меню дублирует инфосообщение"


# ── шлюз не предлагается под ссылку/QR/файл ──────────────────────────────────
async def test_gateway_is_not_offered_for_link_qr_file(services, fake_bot):
    """Сервис выдачу шлюзу отвергает; но кнопки главного меню «Ссылка/QR/Файл»
    и «Выдать конфиг» из карточки профиля всё равно ставили его в список —
    клик вёл в алерт. Теперь в пикерах его нет, а профиль с одним шлюзом
    считается без устройств."""
    services.ensure_admin_client()
    ac = services.admin_client()
    pi = services.add_device(ac.id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30")
    for action in ("gen_link", "gen_qr", "gen_file"):
        cb, nav = _acb(fake_bot)
        await ah.self_gen_pick(cb, AdminSelfCB(action=action), services)
        assert any("добавь устройство" in a.lower() for a, _ in cb.answers if a), "шлюз сошёл за устройство"
    cb, nav = _acb(fake_bot)
    await ah.admin_gen_for(cb, ClientCB(action="gen_for", client_id=ac.id), services)
    assert any("нет устройств" in a.lower() for a, _ in cb.answers if a)
    phone = services.add_device(ac.id, "phone")
    cb, nav = _acb(fake_bot)
    await ah.self_gen_pick(cb, AdminSelfCB(action="gen_link"), services)
    _, labels = last_screen(nav)
    assert any("phone" in l for l in labels) and not any("NASPi" in l for l in labels)
    cb, nav = _acb(fake_bot)
    await ah.admin_gen_for(cb, ClientCB(action="gen_for", client_id=ac.id), services)
    _, labels = last_screen(nav)
    assert any("phone" in l for l in labels) and not any("NASPi" in l for l in labels)
    assert phone.device_id != pi.device_id


async def test_main_menu_hides_issue_row_when_only_device_is_the_gateway(services, fake_bot):
    """Шлюз в «Моих устройствах» виден, но ряд «Ссылка/QR-код/Файл» без
    выдаваемого устройства — пустое обещание; появляется с первым обычным."""
    from tests.conftest import FakeState
    services.ensure_admin_client()
    ac = services.admin_client()
    pi = services.add_device(ac.id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30")
    cb, nav = _acb(fake_bot)
    await ah.admin_main_menu(cb, services, FakeState())
    _, labels = last_screen(nav)
    assert "📱 Мои устройства" in labels and "🔗 Ссылка" not in labels
    services.add_device(ac.id, "phone")
    cb, nav = _acb(fake_bot)
    await ah.admin_main_menu(cb, services, FakeState())
    _, labels = last_screen(nav)
    assert "🔗 Ссылка" in labels and "📄 Файл" in labels
