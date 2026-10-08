"""E2E: полный роутер клиента (handlers/client.py) — меню, устройства, выдача
конфигов, добавление/удаление, карточка устройства без ключа, самоблок,
пауза подписки, отсрочка.
"""
import pytest

from awgbot.bot import texts
from awgbot.bot.handlers import client as ch
from awgbot.bot.callbacks import (BlockCB, DelDeviceCB, DeviceCB, GraceCB, Menu, PauseCB,
                                  PresetCB)
from awgbot.core.blocks import ClientBlock, DeviceBlock
from awgbot.util import timeutil
from tests.conftest import FakeCallback, FakeMessage, FakeState, last_screen

pytestmark = pytest.mark.e2e
G = 1024 ** 3


def _cb(bot, uid):
    nav = FakeMessage(chat_id=uid, user_id=uid, bot=bot)
    return FakeCallback(message=nav, user_id=uid, bot=bot), nav


def _fresh(services, client):
    return services.db.get_client(client.id)


def _rows(markup):
    return [[b.text for b in row] for row in markup.inline_keyboard]


async def test_menu_main_and_info_and_devices(services, fake_bot, make_active_client):
    """Три экрана с главной: главная, «💳 Подписка», «📱 Устройства» — каждый
    со своим заголовком и кнопкой, которая с него ведёт дальше."""
    client = make_active_client(tg_id=5000, period_kind="year")
    services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5000)
    await ch.menu_main(cb, cl, services, FakeState())
    text, labels = last_screen(nav)
    assert text.startswith("👋 <b>Клиент</b>\n") and "📱 Устройства" in labels, (text, labels)
    cb, nav = _cb(fake_bot, 5000)
    await ch.menu_info(cb, cl, services)
    text, labels = last_screen(nav)
    assert text.startswith("💳 <b>Подписка:</b> годовая · 🟢 активна") and labels == ["⏸️ Пауза", "⬅️ Назад"]
    cb, nav = _cb(fake_bot, 5000)
    await ch.menu_devices(cb, cl, services)
    text, labels = last_screen(nav)
    assert text == "📱 <b>Устройства</b> · 1 из 3" and labels == ["⚪ d", "➕ Устройство", "⬅️ Назад"], labels
    assert cb.answers


async def test_menu_gen_empty_vs_present(services, fake_bot, make_active_client):
    """Выдача с главной: нет устройств — всплывашка; одно — ссылка сразу,
    экрана выбора нет; несколько — выбор «для какого устройства?»."""
    client = make_active_client(tg_id=5001)
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5001)
    await ch.menu_gen_pick(cb, Menu(action="gen_link"), cl, services)
    assert cb.answers[-1][1] is True                     # нет устройств → алерт
    assert not any(s[0] == "edit_text" for s in nav.sent)
    services.add_device(client.id, "d")
    cb2, nav2 = _cb(fake_bot, 5001)
    await ch.menu_gen_pick(cb2, Menu(action="gen_link"), cl, services)
    assert not any(s[0] == "edit_text" for s in nav2.sent), "выбор из одного устройства"
    links = [s[1] for s in nav2.sent if s[0] == "answer"]
    assert len(links) == 1 and links[0].startswith("<code>vpn://"), nav2.sent
    services.add_device(client.id, "e")
    cb3, nav3 = _cb(fake_bot, 5001)
    await ch.menu_gen_pick(cb3, Menu(action="gen_link"), cl, services)
    text, labels = last_screen(nav3)
    assert text == "🔗 <b>Ссылка</b> — для какого устройства?" and labels == ["⚪ d", "⚪ e", "⬅️ Назад"]


async def test_device_open_own_foreign_app(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5002)
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5002)
    await ch.device_open(cb, DeviceCB(action="open", device_id=dc.device_id), cl, services, FakeState())
    assert any(s[0] == "edit_text" for s in nav.sent)
    other = make_active_client(tg_id=5003)
    foreign = services.add_device(other.id, "чужое")
    for bad_id in (999999, foreign.device_id):           # несуществующее и чужое
        cb2, nav2 = _cb(fake_bot, 5002)
        await ch.device_open(cb2, DeviceCB(action="open", device_id=bad_id), cl, services, FakeState())
        assert cb2.answers[-1][1] is True, "show_alert «не найдено»"
        assert not any(s[0] == "edit_text" for s in nav2.sent)
    app_id = services.db.create_device(client.id, "app", "PUBZ", "PSK", "10.8.0.60", private_key=None)
    cb3, nav3 = _cb(fake_bot, 5002)
    await ch.device_open(cb3, DeviceCB(action="open", device_id=app_id), cl, services, FakeState())
    assert any(s[0] == "edit_text" for s in nav3.sent)


async def test_own_card_carries_the_issue_row_first(services, fake_bot, make_active_client):
    """Карточка своего: ряд выдачи первым рядом (экрана «Как подключить?»
    больше нет), затем имя и лимит, «Другу» и «Блок», удаление и назад."""
    client = make_active_client(tg_id=5009, traffic_limit=100 * G)
    dc = services.add_device(client.id, "iPhone")
    cb, nav = _cb(fake_bot, 5009)
    await ch.device_open(cb, DeviceCB(action="open", device_id=dc.device_id), _fresh(services, client),
                         services, FakeState())
    text, _ = last_screen(nav)
    rows = _rows(nav.sent[-1][2])
    assert rows == [["🔗 Ссылка", "🔳 QR", "📄 Файл"], ["✏️ Имя", "✏️ Лимит"],
                    ["👤 Другу", "🛑 Блок"], ["🗑 Удалить", "⬅️ Назад"]], rows
    head, usage = text.splitlines()
    assert head == "⚪ <b>iPhone</b>" and usage == "Не подключался · 📊 0 из 100 ГБ (лимит профиля)", text


async def test_device_connect_menu_bot_vs_app(services, fake_bot, make_active_client):
    """Кнопка старого образца «Данные для подключения» ведёт в карточку с
    рядом выдачи; у пира без ключа ряда нет — пояснение и удаление."""
    client = make_active_client(tg_id=5003)
    dc = services.add_device(client.id, "d")
    app_id = services.db.create_device(client.id, "app", "PUBY", "PSK", "10.8.0.61", private_key=None)
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5003)
    await ch.device_connect_menu(cb, DeviceCB(action="connect_menu", device_id=dc.device_id), cl, services)
    _, labels = last_screen(nav)
    assert labels[:3] == ["🔗 Ссылка", "🔳 QR", "📄 Файл"], labels
    cb2, nav2 = _cb(fake_bot, 5003)
    await ch.device_connect_menu(cb2, DeviceCB(action="connect_menu", device_id=app_id), cl, services)
    text2, labels2 = last_screen(nav2)
    assert texts.UNMANAGED_DEVICE_LINE in text2 and "🗑 Удалить" in labels2
    assert not any(l in labels2 for l in ("🔗 Ссылка", "🔳 QR", "📄 Файл", "👤 Другу")), \
        "у пира без ключа выдать нечего — только объяснение и удаление"


async def test_gen_from_card_sends_link_with_explanation_in_one_message(
        services, fake_bot, make_active_client):
    """Ссылка и «☝️ Ссылка для …» — одним сообщением с «⬅️ В меню»; меню под
    кнопкой убрано, живым становится сообщение со ссылкой."""
    client = make_active_client(tg_id=5004)
    dc = services.add_device(client.id, "iPhone")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5004)
    await ch.device_gen(cb, DeviceCB(action="gen_link", device_id=dc.device_id), cl, services)
    sent = [s for s in nav.sent if s[0] == "answer"]
    assert len(sent) == 1, "ссылка и пояснение разъехались на два сообщения"
    body, markup = sent[0][1], sent[0][2]
    vpn = services.generate_config(dc.device_id)["vpn"]
    assert body == (f"<code>{vpn}</code>\n\n☝️ Ссылка для iPhone — нажми на неё, чтобы "
                    "скопировать, и вставь в AmneziaVPN"), body
    assert [(b.text, b.callback_data) for r in markup.inline_keyboard for b in r] == [
        ("⬅️ В меню", Menu(action="main").pack())]
    assert nav.deleted, "меню не должно висеть над ссылкой"


async def test_gen_file_and_qr_carry_the_explanation_as_caption(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5010)
    dc = services.add_device(client.id, "iPhone")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5010)
    await ch.device_gen(cb, DeviceCB(action="gen_file", device_id=dc.device_id), cl, services)
    assert [s[1] for s in nav.sent if s[0] == "document"] == ["📄 Для iPhone — импортируй файл в AmneziaVPN"]
    cb, nav = _cb(fake_bot, 5010)
    await ch.device_gen(cb, DeviceCB(action="gen_qr", device_id=dc.device_id), cl, services)
    assert [s[1] for s in nav.sent if s[0] == "animation"] == [
        "🔳 Для iPhone — в AmneziaVPN «＋» → «Создать из QR-кода», наведи камеру"]
    assert not any(s[0] == "answer" for s in nav.sent), "пояснение отдельным сообщением"


async def test_gen_from_menu_app_shows_dialog(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5005)
    app_id = services.db.create_device(client.id, "app", "PUBW", "PSK", "10.8.0.62", private_key=None)
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5005)
    await ch.device_gen(cb, DeviceCB(action="gen_qr", device_id=app_id), cl, services)
    text, labels = last_screen(nav)
    assert text == texts.UNMANAGED_DEVICE_DIALOG and "🗑 Удалить" in labels
    assert not any(s[0] in ("photo", "animation") for s in nav.sent), "QR для пира без ключа"


async def test_edit_device_traffic_flow(services, fake_bot, make_active_client):
    """Лимит устройства: пресеты на месте карточки; «✏️ Другое» — ввод на
    месте, мусор переспрашивается, число — карточка с итогом первой строкой."""
    client = make_active_client(tg_id=5006)                 # профиль без лимита
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    st = FakeState()
    cb, nav = _cb(fake_bot, 5006)
    await ch.client_edit_device_traffic(cb, DeviceCB(action="edit_traffic", device_id=dc.device_id),
                                        cl, services, st)
    text, labels = last_screen(nav)
    assert text == "📊 <b>Лимит трафика устройства «d»</b>"
    assert labels == ["10 ГБ", "50 ГБ", "100 ГБ", "∞", "✏️ Другое", "⬅️ Отмена"], labels
    cb, nav = _cb(fake_bot, 5006)
    await ch.device_limit_preset(cb, PresetCB(kind="devlimit", ref=dc.device_id, val=-1),
                                 cl, services, st)
    assert (await st.get_data())["dev_ref"] == dc.device_id
    m_bad = FakeMessage(text="abc", chat_id=5006, user_id=5006, bot=fake_bot)
    await ch.client_edit_traffic_apply(m_bad, cl, services, st)
    assert [s[1] for s in m_bad.sent if s[0] == "answer"] == [texts.NUMBER_BAD]
    assert services.db.get_device(dc.device_id).traffic_limit == 0
    m_ok = FakeMessage(text="10", chat_id=5006, user_id=5006, bot=fake_bot)
    await ch.client_edit_traffic_apply(m_ok, cl, services, st)
    assert services.db.get_device(dc.device_id).traffic_limit == 10 * G
    card = [s for s in m_ok.sent if s[0] == "answer"][-1][1]
    assert card.split("\n\n", 1)[0] == "✅ Лимит: ∞ → 10 ГБ", card
    assert await st.get_state() is None


async def test_device_limit_preset_applies_in_place_with_the_note(services, fake_bot, make_active_client):
    """Пресет — сразу, карточка на месте с итогом первой строкой; выше лимита
    профиля (старая кнопка) — отказ всплывашкой, лимит не тронут."""
    client = make_active_client(tg_id=5011, traffic_limit=50 * G)
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5011)
    await ch.device_limit_preset(cb, PresetCB(kind="devlimit", ref=dc.device_id, val=10),
                                 cl, services, FakeState())
    text, labels = last_screen(nav)
    assert text.split("\n\n", 1)[0] == "✅ Лимит: ∞ → 10 ГБ", text
    assert labels[0] == "🔗 Ссылка"
    cb, nav = _cb(fake_bot, 5011)
    await ch.device_limit_preset(cb, PresetCB(kind="devlimit", ref=dc.device_id, val=100),
                                 cl, services, FakeState())
    assert cb.answers[-1] == ("⚠️ Не больше 50 ГБ — лимита профиля", True)
    assert services.db.get_device(dc.device_id).traffic_limit == 10 * G


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


async def test_client_card_of_unmanaged_device_explains_the_dead_end(
        services, fake_bot, make_active_client):
    """Клиенту досталось устройство, которое бот не создавал.

    Так бывает после привязки карантинного пира к профилю. Реставрации больше
    нет, значит карточка обязана честно сказать, что ссылки не будет, и не
    предлагать кнопок, ведущих в никуда, — иначе клиент упрётся в тупик молча.
    """
    client = make_active_client(tg_id=5008)
    did = services.db.create_device(client.id, "чужой", "PBX", "PSK", "10.8.0.63",
                                    private_key=None)
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5008)
    await ch.device_open(cb, DeviceCB(action="open", device_id=did), cl, services, FakeState())

    shown = [r for r in nav.sent if r[0] == "edit_text"]
    assert shown, "карточка не отрисовалась"
    text, markup = shown[-1][1], shown[-1][2]
    assert texts.UNMANAGED_DEVICE_LINE in text
    for row in markup.inline_keyboard:
        for b in row:
            assert "restore" not in (b.callback_data or ""), b.text
            assert "gen_" not in (b.callback_data or ""), b.text


async def test_add_device_start_when_full_alerts(services, fake_bot, make_active_client):
    """Кнопки «➕ Устройство» при исчерпанном лимите нет; кнопка старого образца —
    всплывашка с каноном «удали N», без экрана."""
    client = make_active_client(tg_id=5012, device_limit=1)
    services.add_device(client.id, "occupied")
    cl = _fresh(services, client)
    st = FakeState()
    cb, nav = _cb(fake_bot, 5012)
    await ch.device_add_start(cb, DeviceCB(action="add"), cl, services, st)
    assert cb.answers[-1] == ("Лимит исчерпан: чтобы добавить новое, удали 1", True)
    assert not any(s[0] == "edit_text" for s in nav.sent)

async def test_add_device_for_friend_flow(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5013, device_limit=3)
    cl = _fresh(services, client)
    st = FakeState()
    await st.update_data(for_friend=True, dev_name="ДругНоут")
    m_tr = FakeMessage(text="5", chat_id=5013, user_id=5013, bot=fake_bot)
    await ch.device_add_traffic(m_tr, cl, services, st)
    dev = [d for d in services.db.list_devices(client.id) if d.name == "ДругНоут"][0]
    assert dev.friend_status == "pending" and dev.traffic_limit == 5 * G


async def test_delete_device_flow(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5014)
    services.add_device(client.id, "a")
    d2 = services.add_device(client.id, "b")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5014)
    await ch.device_delete_ask(cb, DelDeviceCB(device_id=d2.device_id, stage="ask"), cl, services)
    text, labels = last_screen(nav)
    assert text == ("🗑 Удалить b?\nСсылка перестанет работать; решишь добавить устройство "
                    "снова — ссылка изменится")
    assert labels == ["⬅️ Отмена", "🗑 Удалить"]
    assert nav.sent[-1][2].inline_keyboard[0][1].style == "danger", "удаление не красное"
    cb2, nav2 = _cb(fake_bot, 5014)
    await ch.device_delete_confirm(cb2, DelDeviceCB(device_id=d2.device_id, stage="confirm"), cl, services)
    assert services.db.get_device(d2.device_id) is None


async def test_delete_only_device_warns_about_losing_the_bot(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5022)
    d = services.add_device(client.id, "MacBook")
    cb, nav = _cb(fake_bot, 5022)
    await ch.device_delete_ask(cb, DelDeviceCB(device_id=d.device_id, stage="ask"),
                               _fresh(services, client), services)
    text, _ = last_screen(nav)
    assert text.startswith("⚠️ Удалить MacBook — единственное устройство?\nVPN выключится сразу"), text


async def test_client_block_unblock_own_device(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5015)
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5015)
    await ch.client_block_device(cb, BlockCB(target="dev", action="menu_block", ref=dc.device_id), cl, services)
    assert int(services.db.get_device(dc.device_id).block_reason) & int(DeviceBlock.USER)
    text, labels = last_screen(nav)
    assert "✅ Разблок" in labels and text.startswith("⛔ <b>d</b>"), (text, labels)
    cb2, nav2 = _cb(fake_bot, 5015)
    await ch.client_unblock_device(cb2, BlockCB(target="dev", action="menu_unblock", ref=dc.device_id), cl, services)
    assert int(services.db.get_device(dc.device_id).block_reason) & int(DeviceBlock.USER) == 0
    assert "🛑 Блок" in last_screen(nav2)[1]


async def test_client_unblock_when_not_user_blocked(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5016)
    dc = services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5016)
    await ch.client_unblock_device(cb, BlockCB(target="dev", action="menu_unblock", ref=dc.device_id), cl, services)
    assert cb.answers[-1][1] is True


async def test_pause_full_cycle(services, fake_bot, make_active_client):
    """Пауза одним экраном: выбор дней = подтверждение, пауза стоит сразу;
    итог — сообщением-следом, экран подписки новым меню. Снятие — без
    вопроса, тоже итог и экран подписки."""
    client = make_active_client(tg_id=5017, period_kind="year")
    services.add_device(client.id, "d")
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5017)
    await ch.pause_ask(cb, PauseCB(action="ask", ref=client.id), cl, services, FakeState())
    text, labels = last_screen(nav)
    assert text.startswith("⏸️ <b>Пауза</b> · до 28 дн.\n") and labels == [
        "7 дн.", "14 дн.", "28 дн.", "✏️ Другое", "⬅️ Отмена"], labels
    assert not _fresh(services, client).is_paused, "экран паузы уже поставил паузу"
    cb2, nav2 = _cb(fake_bot, 5017)
    await ch.pause_pick(cb2, PauseCB(action="pick", ref=client.id, days=7), cl, services, FakeState())
    fresh = _fresh(services, client)
    assert fresh.is_paused and int(fresh.pause_reserved_days) == 7
    until = timeutil.fmt_end_ui(timeutil.parse_iso(fresh.pause_active_since)
                               + __import__("datetime").timedelta(days=7))
    edits = [s for s in nav2.sent if s[0] == "edit_text"]
    assert edits[-1][1] == f"⏸️ Подписка на паузе до {until} — снять раньше можно в разделе «💳 Подписка»"
    assert edits[-1][2] is None, "итог — без кнопок, он остаётся следом"
    screen = [s for s in nav2.sent if s[0] == "answer"][-1]
    # своя пауза видна клиенту — строка состояния доступа над подпиской
    assert screen[1].startswith("🟡 доступ приостановлен\n💳 <b>Подписка:</b> годовая\n"), screen[1]
    assert _rows(screen[2]) == [["▶️ Снять паузу", "⬅️ Назад"]]
    cl2 = _fresh(services, client)
    cb3, nav3 = _cb(fake_bot, 5017)
    await ch.pause_resume(cb3, PauseCB(action="resume", ref=client.id), cl2, services)
    assert not _fresh(services, client).is_paused, "снятие ждёт подтверждения"
    assert [s[1] for s in nav3.sent if s[0] == "edit_text"][-1].startswith("▶️ Пауза снята · ")
    assert cb3.answers[-1] == ("Пауза снята", False)


async def test_pause_other_accepts_a_number_in_range_only(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5023, period_kind="year")
    cl = _fresh(services, client)
    st = FakeState()
    cb, nav = _cb(fake_bot, 5023)
    await ch.pause_other(cb, PauseCB(action="other", ref=client.id), cl, services, st)
    text, labels = last_screen(nav)
    assert text == "✏️ Дней, 1–28" and labels == ["✖️ Отмена"]
    for bad in ("0", "29", "abc"):
        m = FakeMessage(text=bad, chat_id=5023, user_id=5023, bot=fake_bot)
        await ch.pause_other_apply(m, cl, services, st)
        assert [s[1] for s in m.sent if s[0] == "answer"] == ["⚠️ Нужно целое число от 1 до 28"], bad
    assert not _fresh(services, client).is_paused
    m = FakeMessage(text="3", chat_id=5023, user_id=5023, bot=fake_bot)
    await ch.pause_other_apply(m, cl, services, st)
    assert int(_fresh(services, client).pause_reserved_days) == 3


async def test_pause_warning_only_without_mail_escape(services, fake_bot, make_active_client, monkeypatch):
    """«⚠️ На паузе VPN выключен…» — только когда аварийного выхода по почте
    нет: с ним пугать человека нечем, код придёт после паузы."""
    client = make_active_client(tg_id=5024, period_kind="year")
    cl = _fresh(services, client)
    monkeypatch.setattr(services, "email_resume_enabled", lambda: False)
    cb, nav = _cb(fake_bot, 5024)
    await ch.pause_ask(cb, PauseCB(action="ask", ref=client.id), cl, services, FakeState())
    assert texts.PAUSE_WARNING_LINE in last_screen(nav)[0]
    monkeypatch.setattr(services, "email_resume_enabled", lambda: True)
    cb, nav = _cb(fake_bot, 5024)
    await ch.pause_ask(cb, PauseCB(action="ask", ref=client.id), cl, services, FakeState())
    text = last_screen(nav)[0]
    assert "⚠️" not in text and text.startswith("⏸️ <b>Пауза</b> · до 28 дн."), text


async def test_subscription_screen_variants(services, fake_bot, make_active_client):
    """«💳 Подписка»: тип и статус первой строкой, период «с → по» и остаток,
    счёт паузы и правила свёрнутыми, лимиты. Своя пауза — строка паузы и
    «▶️ Снять паузу»; пауза администратора — без кнопки; бессрочная — без
    паузы вовсе."""
    y = make_active_client(tg_id=5030, period_kind="year", device_limit=4, traffic_limit=50 * G)
    c = _fresh(services, y)
    start, end = timeutil.parse_iso(c.period_start), timeutil.parse_iso(c.period_end)
    text, markup = await ch.sub_parts(services, y.id)
    lines = text.splitlines()
    assert lines[0] == "💳 <b>Подписка:</b> годовая · 🟢 активна"
    assert lines[1] == (f"📅 {timeutil.fmt_period_ui(start, end)} · "
                        f"ост. {timeutil.remaining_brief(end)}")
    if start.year != end.year:
        assert lines[1].startswith("📅 " + start.strftime("%d.%m.%y") + " → "), \
            "годовая через границу года — год у начала тоже, иначе даты не читаются"
    assert lines[2] == "" and lines[3] == "⏸️ Пауза: 28 дн. доступно", "пауза — после пустой строки"
    assert lines[4].startswith("<blockquote expandable>+2 дн. паузы") and lines[5].startswith("+28 дн. за продление на год")
    assert lines[-1] == "Включено в подписку: 50 ГБ в месяц · 4 устройства"
    assert _rows(markup) == [["⏸️ Пауза", "⬅️ Назад"]]

    # истекает — жёлтый кружок и дата со временем
    services.db.update_client_fields(y.id, notified_thresholds="10080")
    text, _ = await ch.sub_parts(services, y.id)
    assert text.splitlines()[0] == f"💳 <b>Подписка:</b> годовая · 🟡 истекает {timeutil.fmt_end_ui(end)}"
    services.db.update_client_fields(y.id, notified_thresholds="")

    # своя пауза
    services.enter_pause(y.id, 5)
    text, markup = await ch.sub_parts(services, y.id)
    lines = text.splitlines()
    assert lines[0] == "🟡 доступ приостановлен", "своя пауза — строка состояния доступа над подпиской"
    assert lines[1] == "💳 <b>Подписка:</b> годовая"
    assert lines[3] == "" and lines[4].startswith("⏸️ на паузе с ") and "из 5 дн. — неиспользованный остаток вернётся при досрочном возобновлении" in lines[4]
    assert "ост." not in text, "остаток на паузе не тикает"
    assert _rows(markup) == [["▶️ Снять паузу", "⬅️ Назад"]]

    # пауза администратора — снимает только он
    a = make_active_client(tg_id=5034, period_kind="year")
    services.enter_admin_pause(a.id, 0)
    services._client_set_block(a.id, ClientBlock.PAUSED)
    text, markup = await ch.sub_parts(services, a.id)
    assert text.splitlines()[0] == "🟡 доступ приостановлен", text
    assert text.splitlines()[1] == "💳 <b>Подписка:</b> годовая · ⏸️ приостановлена администратором", text
    assert _rows(markup) == [["⬅️ Назад"]], "кнопка паузы/снятия при паузе администратора"

    # тихая пауза администратора клиенту не видна — ни строки доступа, ни паузы
    q = make_active_client(tg_id=5036, period_kind="year")
    services.enter_admin_pause(q.id, 0)
    services._client_set_block(q.id, ClientBlock.PAUSED | ClientBlock.ADMIN_SILENT)
    text, _ = await ch.sub_parts(services, q.id)
    assert "🟡" not in text and "приостановлен" not in text, \
        f"тихая пауза выдала себя клиенту: {text}"

    # бессрочная — без паузы и без остатка
    n = make_active_client(tg_id=5033, period_kind="never")
    text, markup = await ch.sub_parts(services, n.id)
    assert text.splitlines()[0] == "💳 <b>Подписка:</b> бессрочная · 🟢 активна"
    assert "Пауза" not in text and "ост." not in text
    assert _rows(markup) == [["⬅️ Назад"]]

    # нет дней на счету — кнопки паузы нет
    d = make_active_client(tg_id=5031, period_kind="day")
    _, markup = await ch.sub_parts(services, d.id)
    assert _rows(markup) == [["⬅️ Назад"]], "кнопка паузы без дней на счету"


async def test_subscription_limits_mention_rf_only_when_granted(services, fake_bot, make_active_client):
    """«🇷🇺 РФ-доступ» в строке лимитов — только когда профилю он выдан:
    иначе человек ищет в меню раздел, которого у него нет."""
    c = make_active_client(tg_id=5035, period_kind="year")
    text, _ = await ch.sub_parts(services, c.id)
    assert "🇷🇺" not in text and "РФ" not in text, text
    services.set_routing_allowed(c.id, True)
    text, _ = await ch.sub_parts(services, c.id)
    assert text.splitlines()[-1] == "Включено в подписку: ∞ ГБ в месяц · 3 устройства · 🇷🇺 РФ-доступ", text


async def test_pause_ask_unavailable(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=5018, period_kind="day")     # счёт паузы пуст
    cl = _fresh(services, client)
    cb, nav = _cb(fake_bot, 5018)
    await ch.pause_ask(cb, PauseCB(action="ask", ref=client.id), cl, services, FakeState())
    assert cb.answers[-1][1] is True
    assert not any(s[0] == "edit_text" for s in nav.sent)


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
    text, labels = last_screen(nav)
    assert text == "❓ <b>Помощь</b> — какое устройство?"
    assert labels == ["🍎 iPhone / iPad", "🤖 Android", "🪟 Windows", "🍏 Mac", "⬅️ В меню"], labels
    cb2, nav2 = _cb(fake_bot, 5021)
    await ch.help_skip(cb2, cl, services)
    assert cb2.answers
    assert last_screen(nav2)[0].startswith("👋 <b>Клиент</b>\n"), "«Всё умею сам» возвращает на главную"


async def test_client_renames_own_device(services, fake_bot, make_active_client, monkeypatch):
    """Клиент переименовывает СВОЁ устройство: итог — первой строкой карточки."""
    cl = make_active_client(tg_id=7001, name="Клиент")
    dc = services.add_device(cl.id, "Старое")
    cb, nav = _cb(fake_bot, cl.tg_id)
    st = FakeState()
    await ch.client_device_edit_name_start(cb, DeviceCB(action="edit_name", device_id=dc.device_id),
                                            cl, services, st)
    msg = FakeMessage(text="Новое", chat_id=cl.tg_id, user_id=cl.tg_id, bot=fake_bot)
    await ch.client_device_edit_name_apply(msg, cl, services, st)
    assert services.db.get_device(dc.device_id).name == "Новое"
    card = [s for s in msg.sent if s[0] == "answer"][-1][1]
    assert card.split("\n\n", 1)[0] == "✅ Имя устройства: Старое → Новое", card
    deleted = {r[2] for r in fake_bot.records if r[0] == "delete_message"}
    assert {nav.message_id, msg.message_id} <= deleted, "приглашение или ввод остались в чате"


async def test_client_cannot_rename_foreign_device(services, fake_bot, make_active_client, monkeypatch):
    """Чужое устройство (другого клиента) переименовать нельзя — own_device режет."""
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


# ── РФ-доступ только админу ──────────────

async def test_client_screens_do_not_show_rf_even_when_allowed_and_counted(
        services, fake_bot, make_active_client):
    """РФ-часть трафика — сведения для админа. Клиенту с разрешённым
    РФ-доступом и накопленными байтами карточка устройства и экран подписки
    те же, что без них: иначе вопросы «что за РФ и почему столько»."""
    client = make_active_client(tg_id=5100, name="Ксюша")
    services.db.update_client_fields(client.id, routing_allowed=1)
    dc = services.add_device(client.id, "Телефон")

    async def screens():
        cl = _fresh(services, client)
        cb, nav = _cb(fake_bot, 5100)
        await ch.device_open(cb, DeviceCB(action="open", device_id=dc.device_id), cl, services, FakeState())
        card = last_screen(nav)
        cb, nav = _cb(fake_bot, 5100)
        await ch.menu_info(cb, cl, services)
        return card, last_screen(nav)

    before = await screens()
    services.db.rf_add_bulk([(dc.device_id, 1024 ** 3, 3 * 1024 ** 3)])
    services.db.set_state("rf_month_rx", str(1024 ** 3))
    services.db.set_state("rf_month_tx", str(3 * 1024 ** 3))
    after = await screens()
    assert after == before, "экран клиента изменился от РФ-данных"
    card, sub = after
    assert "🇷🇺" not in card[0], card
    # в подписке «🇷🇺» — только слово в строке лимитов (доступ выдан), без байт
    assert [l for l in sub[0].splitlines() if "🇷🇺" in l] == [
        "Включено в подписку: ∞ ГБ в месяц · 3 устройства · 🇷🇺 РФ-доступ"], sub
