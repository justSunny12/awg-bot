"""E2E: профили глазами администратора — карточка (условные ряды, устройства
в десяти кнопках), «✏️ Изменить» с возвратом после ввода и итогом первой
строкой, период по новому формату, новый профиль пресетами с приглашением
«📤 / 📋», новое приглашение.

Цена ошибки: после ввода — главная вместо «✏️ Изменить», и следующее поле
админ ищет заново; итог не первой строкой — не видно, применилось ли;
приглашение без «📤 / 📋» — ссылку переписывают руками с опечатками;
«✏️ Другое» не возвращает на шаг — профиль с нестандартным лимитом не
создать.
"""
import datetime

import pytest

from awgbot.bot import texts
from awgbot.bot.callbacks import CancelCB, ClientCB, Menu, PeriodCB, PresetCB
from awgbot.bot.handlers import admin as ah
from awgbot.bot.handlers import reply_commands as rc
from awgbot.core import config
from awgbot.util import timeutil
from tests.conftest import FakeCallback, FakeMessage, FakeState, last_screen

pytestmark = pytest.mark.e2e

ADMIN = config.ADMIN_ID
G = 1024 ** 3


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _amsg(bot, text=""):
    return FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=bot)


def _answers(msg):
    return [(t, m) for kind, t, m in msg.sent if kind == "answer"]


def _labels(markup):
    return [b.text for r in markup.inline_keyboard for b in r] if markup else []


# ── карточка ─────────────────────────────────────────────────────────────────

async def test_card_lines(services, fake_bot, make_active_client):
    """Шапка — имя со ссылкой на Telegram и онлайн; подписка одной строкой с
    типом и началом; «📱 N из M»; «📊» — трафик против лимита со стрелками."""
    c = make_active_client("Ксюша", tg_id=4101, period_kind="month", traffic_limit=100 * G)
    d = services.add_device(c.id, "iPhone")
    services.db.add_traffic_bulk([(d.device_id, G, 11 * G)])
    cb, nav = _acb(fake_bot)
    await ah.client_open(cb, ClientCB(action="open", client_id=c.id), services, FakeState())
    text, labels = last_screen(nav)
    lines = text.split("\n")
    c = services.db.get_client(c.id)
    end = timeutil.parse_iso(c.period_end)
    start = timeutil.parse_iso(c.period_start)
    assert lines[0] == '👤 <a href="tg://user?id=4101">Ксюша</a> · ⚪ офлайн', lines
    assert lines[1] == (f"💳 🟢 до {timeutil.fmt_dt_ui(end)} · {timeutil.remaining_brief(end)} · "
                        f"месяц, с {timeutil.fmt_date_ui(start)}"), lines
    assert "📱 1 из 3" in lines, lines
    assert "📊 12 из 100 ГБ (↑1 ↓11)" in lines, lines
    assert not any("Заблокирован" in ln for ln in lines)
    assert labels == ["⏱ Продлить", "✏️ Изменить", "🛑 Блок", "⚪ iPhone", "➕ Устройство", "⬅️ Назад"], labels


async def test_card_collapses_devices_that_do_not_fit_and_lists_them_separately(
        services, fake_bot, make_active_client):
    """Устройств больше, чем влезает в десять рядов, — одна «📱 Устройства:
    N», за ней — отдельный экран со всеми."""
    from awgbot.core.blocks import ClientBlock
    c = make_active_client("Ксюша", tg_id=4102, device_limit=0)
    for i in range(7):
        services.add_device(c.id, f"Тел {i}")
    services.db.update_client_fields(c.id, routing_allowed=1)
    services._client_set_block(c.id, ClientBlock.PAUSED)
    cb, nav = _acb(fake_bot)
    await ah.client_open(cb, ClientCB(action="open", client_id=c.id), services, FakeState())
    _, labels = last_screen(nav)
    rows = [s for s in nav.sent if s[0] == "edit_text"][-1][2].inline_keyboard
    assert len(rows) <= 10, labels
    assert labels[0] == "▶️ Снять паузу" and "📱 Устройства: 7" in labels, labels
    cb2, nav2 = _acb(fake_bot)
    await ah.admin_client_devices(cb2, ClientCB(action="devices", client_id=c.id), services)
    text, labels2 = last_screen(nav2)
    assert text == "📱 <b>Устройства профиля Ксюша</b> · 7", text
    assert [l for l in labels2 if "Тел" in l] == [f"⚪ Тел {i}" for i in range(7)], labels2
    assert labels2[-2:] == ["➕ Устройство", "⬅️ Назад"]


# ── «✏️ Изменить» ─────────────────────────────────────────────────────────────

async def _edit_screen(services, bot, cid):
    cb, nav = _acb(bot)
    await ah.client_edit(cb, ClientCB(action="edit", client_id=cid), services, FakeState())
    return last_screen(nav)


async def test_edit_screen_shows_current_values(services, fake_bot, make_active_client):
    c = make_active_client("Ксюша", tg_id=4201, period_kind="month", traffic_limit=100 * G)
    c = services.db.get_client(c.id)
    text, labels = await _edit_screen(services, fake_bot, c.id)
    s, e = timeutil.parse_iso(c.period_start), timeutil.parse_iso(c.period_end)
    assert text == (f"✏️ <b>Ксюша</b> — изменить\nПериод {timeutil.fmt_period_ui(s, e)} · 3 устройства · "
                    "100 ГБ в месяц"), text
    assert labels == ["✏️ Имя", "✏️ Период", "✏️ Лимит устр-в", "✏️ Трафик", "🗑 Удалить профиль",
                      "⬅️ Назад"], labels
    u = make_active_client("Вечный", tg_id=4202, period_kind="never", device_limit=0)
    text, _ = await _edit_screen(services, fake_bot, u.id)
    assert text == "✏️ <b>Вечный</b> — изменить\nБессрочная · ∞ устройств · ∞ ГБ в месяц", text


async def test_rename_returns_to_edit_with_the_note_first(services, fake_bot, make_active_client):
    """«✏️ Имя» — приглашение на месте экрана с «✖️ Отмена»; пустое имя —
    переспрос; после ввода — «✏️ Изменить» новым живым меню с итогом первой
    строкой, приглашение и ввод убраны."""
    c = make_active_client("Ксюша", tg_id=4203)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    services.db.nav_touch(ADMIN, nav.message_id)
    await ah.edit_name_start(cb, ClientCB(action="edit_name", client_id=c.id), services, st)
    text, labels = last_screen(nav)
    assert text == "✏️ <b>Новое имя для профиля «Ксюша»</b>" and labels == ["✖️ Отмена"], (text, labels)
    empty = _amsg(fake_bot, "  ")
    await ah.edit_name_apply(empty, services, st)
    assert [t for t, _ in _answers(empty)] == [texts.NAME_EMPTY]
    msg = _amsg(fake_bot, "Ксения")
    await ah.edit_name_apply(msg, services, st)
    assert services.db.get_client(c.id).name == "Ксения"
    shown = _answers(msg)
    assert len(shown) == 1, "итог и экран — одним сообщением"
    assert shown[0][0].split("\n")[:3] == ["✅ Имя профиля: Ксюша → Ксения", "", "✏️ <b>Ксения</b> — изменить"], shown
    assert "✏️ Трафик" in _labels(shown[0][1])
    deleted = {r[2] for r in fake_bot.records if r[0] == "delete_message"}
    assert {nav.message_id, empty.message_id, msg.message_id} <= deleted, "приглашение или ввод остались"
    assert await st.get_data() == {}


async def test_cancel_from_an_edit_prompt_goes_back_to_edit(services, fake_bot, make_active_client):
    c = make_active_client("Ксюша", tg_id=4204)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.edit_name_start(cb, ClientCB(action="edit_name", client_id=c.id), services, st)
    cancel = [s for s in nav.sent if s[0] == "edit_text"][-1][2].inline_keyboard[0][0].callback_data
    assert cancel == CancelCB(kind="edit", ref=c.id).pack()
    cb2, nav2 = _acb(fake_bot)
    await rc.on_cancel_inline(cb2, CancelCB.unpack(cancel), st, services, role="admin")
    text, _ = last_screen(nav2)
    assert text.startswith("✏️ <b>Ксюша</b> — изменить\n"), text
    assert services.db.get_client(c.id).name == "Ксюша" and await st.get_data() == {}


async def test_period_edit_two_prompts_new_format_and_note(services, fake_bot, make_active_client):
    """«✏️ Период»: начало, потом окончание — в формате ДД.ММ.ГГГГ ЧЧ:ММ, «-» —
    не менять, «0» у окончания — бессрочно; мусор — переспрос; итог —
    первой строкой «✏️ Изменить»."""
    c = make_active_client("Ксюша", tg_id=4205, period_kind="month")
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.edit_period_start(cb, ClientCB(action="edit_period", client_id=c.id), services, st)
    text, labels = last_screen(nav)
    assert text.startswith("📅 <b>Начало периода профиля Ксюша</b> · сейчас "), text
    assert text.endswith("Введи дату в формате <code>ДД.ММ.ГГГГ ЧЧ:ММ</code> (без времени — 00:00), «-» — не менять")
    assert labels == ["✖️ Отмена"]
    bad = _amsg(fake_bot, "вчера")
    await ah.edit_period_start_apply(bad, services, st)
    assert [t for t, _ in _answers(bad)] == [texts.PERIOD_BAD]
    year = timeutil.now().year + 1
    start = _amsg(fake_bot, f"01.02.{year}")
    await ah.edit_period_start_apply(start, services, st)
    prompt = _answers(start)[-1][0]
    assert prompt.startswith("📅 <b>Окончание периода профиля Ксюша</b> · сейчас ") and \
        prompt.endswith("Дата в том же формате, «-» — не менять, «0» — бессрочно"), prompt
    end = _amsg(fake_bot, f"01.03.{year} 18:30")
    await ah.edit_period_end_apply(end, services, st)
    fresh = services.db.get_client(c.id)
    assert timeutil.parse_iso(fresh.period_end) == datetime.datetime(year, 3, 1, 18, 30, tzinfo=timeutil.TZ)
    shown = _answers(end)[-1][0]
    yy = str(year)[2:]
    assert shown.split("\n")[0] == f"✅ Период: 01.02.{yy} 00:00 → 01.03.{yy} 18:30", shown
    assert shown.split("\n")[2].startswith("✏️ <b>Ксюша</b> — изменить"), shown


async def test_period_end_zero_makes_it_unlimited(services, fake_bot, make_active_client):
    c = make_active_client("Ксюша", tg_id=4206, period_kind="month")
    st = FakeState()
    cb, _ = _acb(fake_bot)
    await ah.edit_period_start(cb, ClientCB(action="edit_period", client_id=c.id), services, st)
    await ah.edit_period_start_apply(_amsg(fake_bot, "-"), services, st)
    end = _amsg(fake_bot, "0")
    await ah.edit_period_end_apply(end, services, st)
    assert services.db.get_client(c.id).period_end is None
    assert _answers(end)[-1][0].split("\n")[0].endswith("→ бессрочно")


# ── новый профиль ────────────────────────────────────────────────────────────

async def test_new_profile_name_presets_invite_note_and_card(services, fake_bot):
    """Имя → три шага пресетами → приглашение с «📤 Отправить» / «📋
    Скопировать», след «✅ Имя: …» и карточка профиля живым меню."""
    services.ensure_admin_client()
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.add_client_start(cb, services, st)
    text, labels = last_screen(nav)
    assert text == "➕ <b>Новый профиль</b> — как назвать?" and labels == ["✖️ Отмена"], (text, labels)

    name = _amsg(fake_bot, "Ксюша")
    await ah.add_client_name(name, services, st)
    text, markup = _answers(name)[-1]
    assert text == "➕ <b>Ксюша</b> — сколько устройств?", text
    assert _labels(markup) == ["1", "2", "3", "5", "10", "∞", "✏️ Другое", "✖️ Отмена"], _labels(markup)

    cb2, nav2 = _acb(fake_bot)
    await ah.add_client_devs_preset(cb2, PresetCB(kind="new_devs", val=3), services, st)
    text, labels = last_screen(nav2)
    assert text.startswith("➕ <b>Ксюша</b> · 3 устройства — трафик в месяц?"), text
    assert labels == ["50 ГБ", "100 ГБ", "200 ГБ", "500 ГБ", "∞", "✏️ Другое", "✖️ Отмена"], labels

    cb3, nav3 = _acb(fake_bot)
    await ah.add_client_traffic_preset(cb3, PresetCB(kind="new_traffic", val=100), services, st)
    text, labels = last_screen(nav3)
    assert text == "➕ <b>Ксюша</b> · 3 устройства · 100 ГБ — срок подписки?", text
    assert labels == ["День", "Неделя", "Месяц", "Год", "∞", "✖️ Отмена"], labels

    cb4, nav4 = _acb(fake_bot)
    await ah.add_client_period(cb4, PeriodCB(kind="month", ctx="create"), services, st)
    c = next(x for x in services.db.list_clients(include_service=False) if x.name == "Ксюша")
    assert (c.device_limit, int(c.traffic_limit), c.period_kind) == (3, 100 * G, "month")
    sent = _answers(nav4)
    invite, note, card = sent[-3], sent[-2], sent[-1]
    assert "https://t.me/test_bot?start=" in invite[0], invite
    assert _labels(invite[1]) == ["📤 Отправить", "📋 Скопировать"], _labels(invite[1])
    end = timeutil.parse_iso(c.period_end)
    assert note[0] == f"✅ Ксюша: 3 устройства, 100 ГБ в месяц, подписка до {timeutil.fmt_dt_ui(end)}", note
    assert note[1] is None, "след без кнопок"
    assert card[0].startswith("👤 ") and card[1] is not None, "карточка профиля не пришла живым меню"
    assert services.db.get_nav_message_id(ADMIN) not in (None, nav4.message_id)
    assert await st.get_data() == {}


async def test_new_profile_other_number_comes_back_to_the_flow(services, fake_bot):
    """«✏️ Другое» на шаге устройств и трафика: ввод числа на месте экрана с
    «✖️ Отмена», мусор — переспрос, число — следующий шаг тем же порядком."""
    services.ensure_admin_client()
    st = FakeState()
    await ah.add_client_name(_amsg(fake_bot, "Петя"), services, st)
    cb, nav = _acb(fake_bot)
    await ah.add_client_devs_preset(cb, PresetCB(kind="new_devs", val=-1), services, st)
    text, labels = last_screen(nav)
    assert text == texts.OTHER_NUMBER_PROMPT and labels == ["✖️ Отмена"], (text, labels)
    bad = _amsg(fake_bot, "семь")
    await ah.add_client_limit(bad, services, st)
    assert [t for t, _ in _answers(bad)] == [texts.NUMBER_BAD_LIMIT]
    seven = _amsg(fake_bot, "7")
    await ah.add_client_limit(seven, services, st)
    assert _answers(seven)[-1][0].startswith("➕ <b>Петя</b> · 7 устройств — трафик в месяц?")

    cb2, nav2 = _acb(fake_bot)
    await ah.add_client_traffic_preset(cb2, PresetCB(kind="new_traffic", val=-1), services, st)
    assert last_screen(nav2)[0] == texts.OTHER_NUMBER_PROMPT
    t = _amsg(fake_bot, "70")
    await ah.add_client_traffic(t, services, st)
    text, markup = _answers(t)[-1]
    assert text == "➕ <b>Петя</b> · 7 устройств · 70 ГБ — срок подписки?", text
    assert "∞" in _labels(markup)
    data = await st.get_data()
    assert (data["limit"], data["traffic_gb"]) == (7, 70)


async def test_new_profile_with_a_stale_dialog_does_not_create_anything(services, fake_bot):
    services.ensure_admin_client()
    before = len(services.db.list_clients(include_service=False))
    cb, nav = _acb(fake_bot)
    await ah.add_client_period(cb, PeriodCB(kind="month", ctx="create"), services, FakeState())
    assert cb.answers and cb.answers[0][1] is True
    assert len(services.db.list_clients(include_service=False)) == before
    assert _answers(nav)[-1][0].startswith("🛠 "), "после устаревшего диалога — главная"


async def test_regen_invite_sends_share_buttons_and_leads_back_to_the_card(
        services, fake_bot):
    """Новое приглашение: сообщение с «📤 / 📋» и финишер «☝️ Приглашение
    для профиля … — работает до активации» с выходом в карточку профиля."""
    created = services.create_client("Ждёт", 1, "year")
    cb, nav = _acb(fake_bot)
    await ah.regen_invite(cb, ClientCB(action="regen_invite", client_id=created.client_id), services)
    sent = _answers(nav)
    assert _labels(sent[-2][1]) == ["📤 Отправить", "📋 Скопировать"], sent
    assert sent[-1][0] == "☝️ Приглашение для профиля Ждёт — работает до активации", sent[-1]
    back = [(b.text, b.callback_data) for r in sent[-1][1].inline_keyboard for b in r]
    assert back == [("👤 В карточку", ClientCB(action="open", client_id=created.client_id).pack()),
                    ("⬅️ На главную", Menu(action="main").pack())], back


async def test_deleting_a_profile_leaves_a_note_and_opens_profiles(services, fake_bot, make_active_client):
    c = make_active_client("Ксюша", tg_id=4301)
    services.add_device(c.id, "Тел")
    cb, nav = _acb(fake_bot)
    await ah.client_delete_apply(cb, ClientCB(action="delete_yes", client_id=c.id), services)
    note = [s for s in nav.sent if s[0] == "edit_text"][-1]
    assert note[1] == "🗑 Профиль Ксюша удалён · устройств удалено: 1" and note[2] is None, note
    listing = _answers(nav)[-1]
    assert listing[0].startswith("👥 <b>Профили</b> · 0"), listing
    assert Menu(action="main").pack() in [b.callback_data for r in listing[1].inline_keyboard for b in r]


# ── карточка: строки состояния доступа, подписки, трафика, переезда ─────────

async def _card(services, bot, cid):
    cb, nav = _acb(bot)
    await ah.client_open(cb, ClientCB(action="open", client_id=cid), services, FakeState())
    text, labels = last_screen(nav)
    markup = [s for s in nav.sent if s[0] == "edit_text"][-1][2]
    return text.split("\n"), labels, markup


def _sub_index(lines):
    return next(i for i, ln in enumerate(lines) if ln.startswith("💳 "))


async def test_card_blocked_line_lists_only_manual_reasons(services, fake_bot, make_active_client):
    """«⛔ Заблокирован» — только ручные причины; исчерпанный трафик — не
    «⛔», а строкой «🟡 исчерпан лимит трафика за месяц» над «💳». Смешай их —
    админ снимет блок, думая, что снял свой, а доступ так и не вернётся."""
    from awgbot.core.blocks import ClientBlock
    c = make_active_client("Ксюша", tg_id=4401, traffic_limit=100 * G)
    services._client_set_block(c.id, ClientBlock.TRAFFIC_CLIENT | ClientBlock.ADMIN_NOTIFIED)
    lines, _, _ = await _card(services, fake_bot, c.id)
    blocked = [ln for ln in lines if ln.startswith("⛔")]
    assert blocked == ["⛔ Заблокирован: администратором"], lines
    i = _sub_index(lines)
    assert lines[i - 1] == "🟡 исчерпан лимит трафика за месяц", lines


async def test_card_traffic_exhausted_without_manual_block_has_no_blocked_line(
        services, fake_bot, make_active_client):
    from awgbot.core.blocks import ClientBlock
    c = make_active_client("Ксюша", tg_id=4402, traffic_limit=100 * G)
    services._client_set_block(c.id, ClientBlock.TRAFFIC_CLIENT)
    lines, _, _ = await _card(services, fake_bot, c.id)
    assert not any(ln.startswith("⛔") for ln in lines), f"трафик выдан за ручную блокировку: {lines}"
    assert "🟡 исчерпан лимит трафика за месяц" in lines, lines


async def test_card_own_pause_line_and_access_line(services, fake_bot, make_active_client):
    """Своя пауза клиента: «🟡 доступ приостановлен» над «💳 ⏸️ на паузе до ДД.ММ»
    — без «(пауза клиента)» и без «⛔»: это не блокировка."""
    c = make_active_client("Ксюша", tg_id=4403, period_kind="year")
    services.enter_pause(c.id, 5)
    fresh = services.db.get_client(c.id)
    until = timeutil.fmt_date_ui(timeutil.parse_iso(fresh.pause_active_since) + datetime.timedelta(days=5))
    lines, _, _ = await _card(services, fake_bot, c.id)
    i = _sub_index(lines)
    assert lines[i - 1:i + 1] == ["🟡 доступ приостановлен", f"💳 ⏸️ на паузе до {until}"], lines
    assert not any(ln.startswith("⛔") for ln in lines), lines


async def test_card_admin_pause_says_who_paused(services, fake_bot, make_active_client):
    from awgbot.core.blocks import ClientBlock
    c = make_active_client("Ксюша", tg_id=4404, period_kind="year")
    services.enter_admin_pause(c.id, 0)
    services._client_set_block(c.id, ClientBlock.PAUSED)
    lines, _, _ = await _card(services, fake_bot, c.id)
    i = _sub_index(lines)
    assert lines[i - 1:i + 1] == ["🟡 доступ приостановлен", "💳 ⏸️ приостановлена администратором"], lines


async def test_card_expired_subscription_shows_the_date_and_the_access_line(
        services, fake_bot, make_active_client):
    c = make_active_client("Ксюша", tg_id=4405, period_kind="month")
    services.db.update_client_fields(c.id, period_end="2026-09-01T00:00:00+03:00", status="expired")
    lines, _, _ = await _card(services, fake_bot, c.id)
    i = _sub_index(lines)
    assert lines[i - 1] == "🟡 доступ приостановлен", lines
    assert lines[i].startswith("💳 🔴 истекла 01.09"), lines


async def test_active_card_has_no_access_line(services, fake_bot, make_active_client):
    """Всё в порядке — строки «🟡» нет: иначе она перестаёт что-то значить."""
    c = make_active_client("Ксюша", tg_id=4406)
    lines, _, _ = await _card(services, fake_bot, c.id)
    assert not any(ln.startswith("🟡") for ln in lines), lines


@pytest.mark.parametrize("limit, expected", [(100 * G, "📊 0 из 100 ГБ"), (0, "📊 0 ГБ (безлимит)")])
async def test_card_zero_traffic_line(services, fake_bot, make_active_client, limit, expected):
    """Нуль трафика — строка стоит («0 из 100 ГБ» / «0 ГБ (безлимит)») без стрелок:
    пропавшая строка читается как «учёт сломан», а «↑0 ↓0» — шум."""
    c = make_active_client("Ксюша", tg_id=4407, traffic_limit=limit)
    lines, _, _ = await _card(services, fake_bot, c.id)
    assert [ln for ln in lines if ln.startswith("📊")] == [expected], lines


def test_card_migration_line_goes_last_after_a_blank_line(services, make_active_client):
    """«🚚 Переезд» — временная строка: отдельным блоком в самом конце, чтобы не
    смешиваться с постоянными строками карточки; нет живых устройств — нет строки."""
    c = make_active_client("Ксюша", tg_id=4408)
    d = services.client_card_data(c.id)
    d["progress"] = (1, 2, 3)
    out = texts.admin_client_card(d).split("\n")
    assert out[-2] == "" and out[-1].startswith("🚚 Переезд: 1 из 2"), out
    d["progress"] = (0, 0, 3)
    assert "Переезд" not in texts.admin_client_card(d)


# ── у профиля админа карточки нет ────────────────────────────────────────────

async def test_admin_profile_has_no_card_and_links_lead_home(services, fake_bot):
    """Над собой админ не продлевает, не блокирует и лимитов не ставит —
    карточка была набором запрещённых кнопок. Открытие по кнопке и по ссылке
    cl-<id> ведёт на главную, имя профиля админа в текстах — не ссылка."""
    from awgbot.bot.handlers.admin.clients import client_card_parts
    services.bot_username = "awg_test_bot"
    services.ensure_admin_client()
    ac = services.admin_client()
    assert await client_card_parts(services, ac.id) is None
    cb, nav = _acb(fake_bot)
    await ah.client_open(cb, ClientCB(action="open", client_id=ac.id), services, FakeState())
    text, _ = last_screen(nav)
    assert text.startswith("🛠 "), f"карточка профиля админа открылась: {text[:60]}"
    services.db.set_nav_message_id(ADMIN, None)
    from aiogram.filters import CommandObject
    msg = _amsg(fake_bot, f"/start cl-{ac.id}")
    await ah.admin_start(msg, services, FakeState(),
                         command=CommandObject(prefix="/", command="start", args=f"cl-{ac.id}"))
    shown = _answers(msg)
    assert shown and shown[-1][0].startswith("🛠 "), shown
    assert texts.profile_link(ac, "awg_test_bot") == ac.name
    assert services.cl_link(ac) == ac.name, "ссылка на несуществующую карточку админа"


async def test_profile_links_and_back_button_for_a_regular_profile(services, fake_bot, make_active_client):
    """У обычного профиля имя — ссылка cl-<id>, «⬅️ Назад» карточки — к списку профилей."""
    services.bot_username = "awg_test_bot"
    c = make_active_client("Ксюша", tg_id=4409)
    assert texts.profile_link(c, "awg_test_bot") == \
        f'<a href="https://t.me/awg_test_bot?start=cl-{c.id}">Ксюша</a>'
    _, _, markup = await _card(services, fake_bot, c.id)
    back = markup.inline_keyboard[-1][-1]
    assert (back.text, back.callback_data) == ("⬅️ Назад", Menu(action="clients").pack())


# ── карточка устройства у админа ─────────────────────────────────────────────

@pytest.mark.parametrize("dev_limit, profile_limit, used, expected", [
    (50, 0, True, "📊 3.2 из 50 ГБ (↑0.4 ↓2.8)"),
    (0, 100, True, "📊 3.2 из 100 ГБ (↑0.4 ↓2.8), лимит профиля"),
    (50, 100, True, "📊 3.2 из 50 ГБ (↑0.4 ↓2.8)"),          # свой лимит важнее профильного
    (0, 0, True, "📊 3.2 ГБ (↑0.4 ↓2.8)"),
    (50, 0, False, "📊 0 из 50 ГБ"),
    (0, 0, False, "📊 0 ГБ"),
])
async def test_admin_device_card_usage_line(services, make_active_client, dev_limit, profile_limit,
                                            used, expected):
    """Строка трафика устройства: чей лимит — сказано, если профильный; нули
    без стрелок. Спутай лимиты — админ решит, что устройству отведено 100 ГБ,
    а оно упрётся в свои 50."""
    from awgbot.bot.handlers.admin.devices import device_card_parts
    c = make_active_client("Ксюша", tg_id=4501, traffic_limit=profile_limit * G)
    dev_id = services.add_device(c.id, "iPhone").device_id
    if dev_limit:
        services.db.update_device_fields(dev_id, traffic_limit=dev_limit * G)
    if used:
        services.db.add_traffic_bulk([(dev_id, int(0.4 * G), int(2.8 * G))])
    text, _ = await device_card_parts(services, services.db.get_device(dev_id))
    usage = text.split("\n")[1]
    assert usage == f"Не подключался · {expected}", text


async def test_admin_device_card_states_go_in_a_separate_block(services, make_active_client):
    """Блокировка и прочие состояния — отдельным блоком через пустую строку,
    а не хвостом к трафику: иначе «⛔» теряется среди цифр."""
    from awgbot.bot.handlers.admin.devices import device_card_parts
    from awgbot.core.blocks import DeviceBlock
    c = make_active_client("Ксюша", tg_id=4502)
    dev_id = services.add_device(c.id, "iPhone").device_id
    text, _ = await device_card_parts(services, services.db.get_device(dev_id))
    assert "" not in text.split("\n"), f"пустая строка без состояний:\n{text}"
    services._device_set_block(dev_id, DeviceBlock.ADMIN_NOTIFIED)
    text, _ = await device_card_parts(services, services.db.get_device(dev_id))
    lines = text.split("\n")
    assert lines[-2:] == ["", "⛔ Заблокировано: администратором"], text


# ── раскладка пресетов ───────────────────────────────────────────────────────

def _rows(markup):
    return [[b.text for b in r] for r in markup.inline_keyboard]


def test_device_presets_three_in_a_row_with_cancel_in_the_last_row():
    """Устройства: [1][2][3] / [5][10][∞] / [✏️ Другое][Отмена] — отмена
    садится в последний ряд, где есть место; лишний ряд из одной кнопки —
    лишний экран прокрутки на телефоне."""
    from awgbot.bot import keyboards as kb
    assert _rows(kb.devs_limit_kb(1)) == [["1", "2", "3"], ["5", "10", "∞"], ["✏️ Другое", "⬅️ Отмена"]]
    assert _rows(kb.new_profile_devs_kb()) == [["1", "2", "3"], ["5", "10", "∞"], ["✏️ Другое", "✖️ Отмена"]]


def test_traffic_presets_put_cancel_on_its_own_row_when_the_last_is_full():
    """Трафик: [50][100][200] / [500][∞][✏️ Другое] / [Отмена] — ряд полон,
    отмена своим рядом, а не четвёртой кнопкой с обрезанными подписями."""
    from awgbot.bot import keyboards as kb
    assert _rows(kb.traffic_limit_kb(1)) == [["50 ГБ", "100 ГБ", "200 ГБ"], ["500 ГБ", "∞", "✏️ Другое"],
                                             ["⬅️ Отмена"]]
    assert _rows(kb.new_profile_traffic_kb())[-1] == ["✖️ Отмена"]


def test_preset_cancel_leads_back_to_edit_and_other_asks_a_number():
    from awgbot.bot import keyboards as kb
    m = kb.devs_limit_kb(7)
    flat = {b.text: b.callback_data for r in m.inline_keyboard for b in r}
    assert flat["⬅️ Отмена"] == ClientCB(action="edit", client_id=7).pack()
    assert flat["✏️ Другое"] == PresetCB(kind="cli_devs", ref=7, val=-1).pack()
    assert flat["∞"] == PresetCB(kind="cli_devs", ref=7, val=0).pack(), "∞ — безлимит (0)"


# ── перенос и блокировка устройства: чьё оно ─────────────────────────────────

def _cl(c):
    return f'<a href="https://t.me/awg_test_bot?start=cl-{c.id}">{c.name}</a>'


async def test_reassign_names_the_device_and_both_profiles(services, fake_bot, make_active_client):
    """Вопрос переноса и итог называют устройство в кавычках и профили
    ссылками: «iPhone» у каждого второго — без владельца админ не отличит,
    чей телефон он переносит и куда тот ушёл."""
    from awgbot.bot.callbacks import DeviceCB, ReassignCB
    services.bot_username = "awg_test_bot"
    kolya = make_active_client("Коля", tg_id=4601)
    ksu = make_active_client("Ксюша", tg_id=4602)
    dc = services.add_device(kolya.id, "iPhone")
    cb, nav = _acb(fake_bot)
    await ah.device_reassign_start(cb, DeviceCB(action="reassign", device_id=dc.device_id), services)
    text, _ = last_screen(nav)
    assert text == f"🔀 <b>Перенос устройства «iPhone»</b> ({_cl(kolya)}) — в какой профиль?", text
    cb, nav = _acb(fake_bot)
    await ah.device_reassign_apply(cb, ReassignCB(device_id=dc.device_id, client_id=ksu.id, stage="go"),
                                   services)
    text, _ = last_screen(nav)
    assert text.split("\n")[0] == f"✅ устройство «iPhone» перенесено: {_cl(kolya)} → {_cl(ksu)}", text


async def test_reassign_of_an_unassigned_peer_says_without_profile(services, fake_bot, make_active_client):
    from awgbot.bot.callbacks import DeviceCB, ReassignCB
    services.bot_username = "awg_test_bot"
    ksu = make_active_client("Ксюша", tg_id=4603)
    svc = services.db.get_service_client_id()
    dev_id = services.db.create_device(svc, "app", "PUBKEYAPP", "PSK", "10.8.0.71")
    cb, nav = _acb(fake_bot)
    await ah.device_reassign_start(cb, DeviceCB(action="reassign", device_id=dev_id), services)
    text, _ = last_screen(nav)
    assert text == "🔀 <b>Перенос устройства «app»</b> — в какой профиль?", text
    cb, nav = _acb(fake_bot)
    await ah.device_reassign_apply(cb, ReassignCB(device_id=dev_id, client_id=ksu.id, stage="go"), services)
    shown = [s for s in nav.sent if s[0] == "edit_text"]
    assert shown and shown[-1][1].split("\n")[0] == f"✅ устройство «app» перенесено: без профиля → {_cl(ksu)}", \
        shown[-1][1]


async def test_block_toast_names_the_owner_in_plain_text(services, fake_bot, make_active_client):
    """Всплывашка — простой текст (ссылки в ней не работают): «🛑 Устройство
    «iPhone» (Петя) заблокировано», тихо — с «(тихо)»."""
    from awgbot.bot.callbacks import BlockCB
    services.bot_username = "awg_test_bot"
    petya = make_active_client("Петя", tg_id=4604)
    for kind, tail in (("notified", ""), ("silent", " (тихо)")):
        dc = services.add_device(petya.id, f"iPhone{kind[0]}")
        cb, _ = _acb(fake_bot)
        await ah.admin_block_do(cb, BlockCB(target="dev", action="block", ref=dc.device_id, kind=kind),
                                services)
        assert cb.answers[-1][0] == f"🛑 Устройство «iPhone{kind[0]}» (Петя) заблокировано{tail}", cb.answers
