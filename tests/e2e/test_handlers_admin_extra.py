"""E2E: добор веток роутера админа — лимит устройств пресетами без
подтверждения, продление, выдача файла, блок устройства, личные qr/file,
устройство профилю, «Мои устройства», карточка пира без приватного ключа.
Главная, ссылки и трафик — test_admin_home.py и test_admin_traffic_tree.py.

Объявления — test_handlers_broadcast.py; почта и бэкапы из чата —
test_handlers_settings_mail_backup.py; раскладка настроек — test_settings_layout.py.
"""

import pytest

from awgbot.bot import texts
from awgbot.bot.handlers import admin as ah
from awgbot.bot.callbacks import AdminSelfCB, BlockCB, ClientCB, DeviceCB
from awgbot.core import config
from tests.conftest import FakeCallback, FakeMessage, FakeState, last_screen

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _amsg(bot, text=""):
    return FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=bot)


# ── понижение лимита ниже числа устройств — без подтверждения ──────────────
async def test_lowering_the_device_limit_by_preset_applies_at_once_with_a_warning(
        services, fake_bot, make_active_client):
    """Лимит ниже числа устройств — без диалога подтверждения: «✏️ Изменить»
    с итогом первой строкой «✅ Устройств: 5 → 1 · ⚠️ сейчас 2 из 1 — …».
    Потеряй строку ⚠️ — админ не узнает, что профиль теперь не сможет добавить
    ни одного устройства; верни подтверждение — лишний шаг на каждое изменение."""
    from awgbot.bot.callbacks import PresetCB
    client = make_active_client(tg_id=6300, device_limit=5)
    services.add_device(client.id, "a")
    services.add_device(client.id, "b")
    cb, nav = _acb(fake_bot)
    await ah.edit_limit_start(cb, ClientCB(action="edit_limit", client_id=client.id), services, FakeState())
    text, labels = last_screen(nav)
    assert text == f"🔢 Лимит устройств профиля {client.name} · сейчас 5, занято 2", text
    assert labels == ["1", "2", "3", "5", "10", "∞", "✏️ Другое", "⬅️ Отмена"], labels

    cb2, nav2 = _acb(fake_bot)
    await ah.edit_limit_preset(cb2, PresetCB(kind="cli_devs", ref=client.id, val=1), services, FakeState())
    assert services.db.get_client(client.id).device_limit == 1, "лимит не применён сразу"
    text2, labels2 = last_screen(nav2)
    first = text2.splitlines()[0]
    assert first == "✅ Устройств: 5 → 1 · ⚠️ сейчас 2 из 1 — новые не добавить, пока не станет меньше", text2
    assert "✏️ Лимит устр-в" in labels2, "после пресета — не «✏️ Изменить»"
    notes = [r for r in fake_bot.records if r[0] == "send_message" and r[1] == 6300]
    assert len(notes) == 1, "клиент должен узнать о новом лимите ровно одним уведомлением"


async def test_typed_device_limit_returns_to_edit_with_the_note(services, fake_bot, make_active_client):
    """«✏️ Другое» → число → «✏️ Изменить» новым сообщением с итогом первой
    строкой; повышение — без строки ⚠️."""
    from awgbot.bot.callbacks import PresetCB
    client = make_active_client(tg_id=6301, device_limit=2)
    services.add_device(client.id, "a")
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.edit_limit_preset(cb, PresetCB(kind="cli_devs", ref=client.id, val=-1), services, st)
    text, labels = last_screen(nav)
    assert text == texts.OTHER_NUMBER_PROMPT and labels == ["✖️ Отмена"], (text, labels)
    bad = _amsg(fake_bot, "много")
    await ah.edit_limit_apply(bad, services, st)
    assert [s[1] for s in bad.sent if s[0] == "answer"] == [texts.NUMBER_BAD_LIMIT]
    assert services.db.get_client(client.id).device_limit == 2, "мусор применился как лимит"
    ok = _amsg(fake_bot, "7")
    await ah.edit_limit_apply(ok, services, st)
    assert services.db.get_client(client.id).device_limit == 7
    shown = [s for s in ok.sent if s[0] == "answer"]
    assert shown and shown[-1][1].splitlines()[0] == "✅ Устройств: 2 → 7", shown[-1][1]
    assert shown[-1][1].splitlines()[2].startswith(f"✏️ {client.name} — изменить"), shown[-1][1]
    assert "⚠️" not in shown[-1][1]
    assert await st.get_data() == {}, "диалог не закрыт"


# ── продление / файл ─────────────────────────────────────────────────────────
async def test_extend_start_renders(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6303, period_kind="year")
    cb, nav = _acb(fake_bot)
    await ah.extend_start(cb, ClientCB(action="extend", client_id=client.id), services, FakeState())
    text, labels = last_screen(nav)
    assert text.startswith(f"⏱ Продление: {client.name}\nСейчас до "), text
    assert labels[:5] == ["День", "Неделя", "Месяц", "Год", "∞"], labels
    assert labels[-1] == "⬅️ Отмена", "диалог выбора срока — не тупик"


async def test_admin_dev_file(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6304)
    dc = services.add_device(client.id, "d")
    cb, nav = _acb(fake_bot)
    await ah.admin_dev_gen(cb, DeviceCB(action="gen_file", device_id=dc.device_id), services)
    assert any(s[0] == "document" for s in nav.sent)


async def test_block_menu_device_branch(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6306)
    dc = services.add_device(client.id, "d")
    cb, nav = _acb(fake_bot)
    await ah.admin_block_menu(cb, BlockCB(target="dev", action="menu_block", ref=dc.device_id), services)
    text, labels = last_screen(nav)
    assert text == "🛑 Как заблокировать d?", text
    assert labels == ["🔔 С уведомлением", "🔕 Тихо", "⬅️ Отмена"], labels


# ── личные qr/file, устройство профилю ───────────────────────────────────────
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


async def test_add_device_to_a_profile_asks_only_the_name_and_returns_to_the_card(
        services, fake_bot, make_active_client):
    """«➕ Устройство» в карточке профиля: одно имя → карточка профиля новым
    сообщением с итогом первой строкой; владельцу — уведомление с рядом выдачи.
    Экрана «Кому добавить» больше нет — устройство профилю добавляется из его
    карточки."""
    client = make_active_client(tg_id=6307, name="Клиент-6307", device_limit=3)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.admin_add_device_start(cb, ClientCB(action="add_device", client_id=client.id), services, st)
    text, labels = last_screen(nav)
    assert text == "➕ Устройство профилю Клиент-6307 · 0 из 3\nКак назвать?", text
    assert labels == ["✖️ Отмена"], labels
    msg = _amsg(fake_bot, "Планшет")
    await ah.admin_add_device_name(msg, services, st)
    assert [d.name for d in services.db.list_devices(client.id)] == ["Планшет"]
    shown = [s for s in msg.sent if s[0] == "answer"]
    assert shown and shown[-1][1].startswith("✅ Планшет: создано для профиля Клиент-6307\n\n👤 "), \
        shown[-1][1]
    owner_notes = [r for r in fake_bot.records if r[0] == "send_message" and r[1] == 6307]
    assert len(owner_notes) == 1 and "Планшет" in owner_notes[0][2]


async def test_add_device_to_a_full_profile_is_refused_with_how_many_to_delete(
        services, fake_bot, make_active_client):
    client = make_active_client(tg_id=6308, device_limit=1)
    services.add_device(client.id, "a")
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.admin_add_device_start(cb, ClientCB(action="add_device", client_id=client.id), services, st)
    assert len(cb.answers) == 1 and cb.answers[0][1] is True, cb.answers
    assert cb.answers[0][0].startswith("Достигнут лимит") and "1 из 1" in cb.answers[0][0], cb.answers
    assert await st.get_state() is None, "ввод имени открыт при исчерпанном лимите"


async def test_admin_menu_devices(services, fake_bot):
    """«📱 Мои устройства · 1, без лимита»; шлюз — вверху с «[шлюз]»."""
    services.ensure_admin_client()
    ac = services.admin_client()
    services.add_device(ac.id, "Ноут")
    pi = services.add_device(ac.id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30")
    cb, nav = _acb(fake_bot)
    await ah.admin_menu_devices(cb, services, FakeState())
    text, labels = last_screen(nav)
    assert text == "📱 Мои устройства · 2, без лимита", text
    assert labels[0] == "🛰 NASPi [шлюз]", labels
    assert any(l.endswith(" Ноут") for l in labels[1:]), labels
    assert labels[-2:] == ["➕ Устройство", "⬅️ Назад"], labels


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
    assert "🔁 Приглашение" in owner, owner
    assert not any("Приглашение" in t for t in admin), admin


async def test_unmanaged_device_card_and_issue_say_there_is_no_link(
        services, fake_bot):
    """Пир без приватного ключа: карточка говорит «ссылки нет», ряда выдачи в
    ней нет, а кнопка старого образца выдачи отвечает тем же диалогом."""
    from awgbot.bot import texts

    svc = services.db.get_service_client_id()
    did = services.db.create_device(svc, "чужой", "PUBQ2", "PSK", "10.8.1.10",
                                    private_key=None)
    cb, nav = _acb(fake_bot)
    await ah.admin_device_connect_menu(cb, DeviceCB(action="connect_menu", device_id=did),
                                       services, FakeState())
    text, labels = last_screen(nav)
    assert "✳️ Добавлено не ботом — ссылки нет" in text, text
    assert not {"🔗 Ссылка", "🔳 QR", "📄 Файл"} & set(labels), labels
    cb2, nav2 = _acb(fake_bot)
    await ah.admin_dev_gen(cb2, DeviceCB(action="gen_link", device_id=did), services)
    assert texts.UNMANAGED_DEVICE_DIALOG in [r[1] for r in nav2.sent if r[0] == "edit_text"]

def _btn_texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def test_transfer_buttons_are_split_by_role(services, make_active_client):
    """Владелец: «👤 Другу», без «🔀 Передать» в другой профиль. Админ: наоборот."""
    from awgbot.bot import keyboards as kbs
    c = make_active_client("Профиль Г")
    services.add_device(c.id, "Ноут")
    dev = services.db.list_devices(c.id)[0]
    owner = [b.text for row in kbs.device_actions(dev, is_admin=False, back_target="x").inline_keyboard for b in row]
    admin = [b.text for row in kbs.device_actions(dev, is_admin=True, back_target="x",
                                                  reassign_label="🔀 Передать в другой профиль").inline_keyboard for b in row]
    assert "👤 Другу" in owner and "🔀 Передать" not in owner
    assert "🔀 Передать" in admin and "👤 Другу" not in admin


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
                for btn in row if btn.callback_data.startswith("bc:tgl:")]
    assert labels(False) == ["☑️ Анна", "☑️ Борис", "☑️ Вера"]
    # год текущий — в дате его нет
    assert labels(True) == ["☑️ Анна", "☑️ Борис ∞", "☑️ Вера ⛔ 01.09"]


def test_device_line_format_and_plain_ip(services, make_active_client):
    from awgbot.bot import texts
    c = make_active_client("Профиль Е")
    services.add_device(c.id, "iPhone 16 Pro")
    dev = services.db.list_devices(c.id)[0]
    line = texts.device_line(dev)
    assert line.startswith(f"🔴 iPhone 16 Pro ({texts.plain_ip(dev.address)}), последний коннект: ")
    assert texts.plain_ip("10.9.1.2") == "<code>10.9.1.2</code>"


# ── шлюз не предлагается под ссылку/QR/файл ──────────────────────────────────
async def test_gateway_is_not_offered_for_link_qr_file(services, fake_bot):
    """Сервис выдачу шлюзу отвергает; кнопки старого образца «Ссылка/QR/Файл»
    ставили его в список — клик вёл в алерт. В пикере его нет, а профиль с
    одним шлюзом считается без устройств. Старая «Выдать конфиг» профиля
    открывает карточку профиля — выдача там рядом с каждым устройством."""
    services.ensure_admin_client()
    ac = services.admin_client()
    pi = services.add_device(ac.id, "NASPi")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30")
    for action in ("gen_link", "gen_qr", "gen_file"):
        cb, nav = _acb(fake_bot)
        await ah.self_gen_pick(cb, AdminSelfCB(action=action), services)
        assert any("добавь устройство" in a.lower() for a, _ in cb.answers if a), "шлюз сошёл за устройство"
    services.add_device(ac.id, "phone")
    cb, nav = _acb(fake_bot)
    await ah.self_gen_pick(cb, AdminSelfCB(action="gen_link"), services)
    _, labels = last_screen(nav)
    assert any("phone" in l for l in labels) and not any("NASPi" in l for l in labels)
    cb, nav = _acb(fake_bot)
    await ah.admin_gen_for(cb, ClientCB(action="gen_for", client_id=ac.id), services, FakeState())
    text, _ = last_screen(nav)
    assert text.startswith("👤 "), f"старая «Выдать конфиг» не привела в карточку: {text}"


async def test_device_limit_other_asks_a_number_and_returns_to_the_card(
        services, fake_bot, make_active_client):
    """«✏️ Другое» у лимита устройства: ввод на месте экрана с «✖️ Отмена»,
    число выше лимита профиля — переспрос, в пределах — карточка устройства
    с итогом первой строкой. Не откроется ввод — лимит вне пресетов не задать."""
    from awgbot.bot.callbacks import PresetCB
    client = make_active_client(tg_id=6309, traffic_limit=50 * 1024 ** 3)
    dc = services.add_device(client.id, "Тел")
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.device_limit_preset(cb, PresetCB(kind="devlimit", ref=dc.device_id, val=-1), services, st)
    text, labels = last_screen(nav)
    assert labels == ["✖️ Отмена"], (text, labels)
    over = _amsg(fake_bot, "70")
    await ah.edit_traffic_apply(over, services, st)
    assert int(services.db.get_device(dc.device_id).traffic_limit) == 0, "лимит выше профиля принят"
    ok = _amsg(fake_bot, "20")
    await ah.edit_traffic_apply(ok, services, st)
    assert int(services.db.get_device(dc.device_id).traffic_limit) == 20 * 1024 ** 3
    shown = [s for s in ok.sent if s[0] == "answer"]
    assert shown and "Тел" in shown[-1][1].split("\n\n")[1], shown
