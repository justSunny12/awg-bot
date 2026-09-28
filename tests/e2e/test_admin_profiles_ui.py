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
    """Устройств больше, чем влезает в десять кнопок, — одна «📱 Устройства:
    N», за ней — отдельный экран со всеми."""
    from awgbot.core.blocks import ClientBlock
    c = make_active_client("Ксюша", tg_id=4102, device_limit=0)
    for i in range(5):
        services.add_device(c.id, f"Тел {i}")
    services.db.update_client_fields(c.id, routing_allowed=1)
    services._client_set_block(c.id, ClientBlock.PAUSED)
    cb, nav = _acb(fake_bot)
    await ah.client_open(cb, ClientCB(action="open", client_id=c.id), services, FakeState())
    _, labels = last_screen(nav)
    assert len(labels) <= 10, labels
    assert labels[0] == "▶️ Снять паузу" and "📱 Устройства: 5" in labels, labels
    cb2, nav2 = _acb(fake_bot)
    await ah.admin_client_devices(cb2, ClientCB(action="devices", client_id=c.id), services)
    text, labels2 = last_screen(nav2)
    assert text == "📱 Устройства профиля Ксюша · 5", text
    assert [l for l in labels2 if "Тел" in l] == [f"⚪ Тел {i}" for i in range(5)], labels2
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
    assert text == (f"✏️ Ксюша — изменить\nПериод {timeutil.fmt_period_ui(s, e)} · 3 устройства · "
                    "100 ГБ в месяц"), text
    assert labels == ["✏️ Имя", "✏️ Период", "✏️ Лимит устр-в", "✏️ Трафик", "🗑 Удалить профиль",
                      "⬅️ Назад"], labels
    u = make_active_client("Вечный", tg_id=4202, period_kind="never", device_limit=0)
    text, _ = await _edit_screen(services, fake_bot, u.id)
    assert text == "✏️ Вечный — изменить\nБессрочная · ∞ устройств · ∞ ГБ в месяц", text


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
    assert text == "✏️ Новое имя для профиля «Ксюша»" and labels == ["✖️ Отмена"], (text, labels)
    empty = _amsg(fake_bot, "  ")
    await ah.edit_name_apply(empty, services, st)
    assert [t for t, _ in _answers(empty)] == [texts.NAME_EMPTY]
    msg = _amsg(fake_bot, "Ксения")
    await ah.edit_name_apply(msg, services, st)
    assert services.db.get_client(c.id).name == "Ксения"
    shown = _answers(msg)
    assert len(shown) == 1, "итог и экран — одним сообщением"
    assert shown[0][0].split("\n")[:3] == ["✅ Имя профиля: Ксюша → Ксения", "", "✏️ Ксения — изменить"], shown
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
    assert text.startswith("✏️ Ксюша — изменить\n"), text
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
    assert text.startswith("📅 Начало периода профиля Ксюша · сейчас "), text
    assert text.endswith("Введи дату в формате <code>ДД.ММ.ГГГГ ЧЧ:ММ</code> (без времени — 00:00), «-» — не менять")
    assert labels == ["✖️ Отмена"]
    bad = _amsg(fake_bot, "вчера")
    await ah.edit_period_start_apply(bad, services, st)
    assert [t for t, _ in _answers(bad)] == [texts.PERIOD_BAD]
    year = timeutil.now().year + 1
    start = _amsg(fake_bot, f"01.02.{year}")
    await ah.edit_period_start_apply(start, services, st)
    prompt = _answers(start)[-1][0]
    assert prompt.startswith("📅 Окончание периода профиля Ксюша · сейчас ") and \
        prompt.endswith("Дата в том же формате, «-» — не менять, «0» — бессрочно"), prompt
    end = _amsg(fake_bot, f"01.03.{year} 18:30")
    await ah.edit_period_end_apply(end, services, st)
    fresh = services.db.get_client(c.id)
    assert timeutil.parse_iso(fresh.period_end) == datetime.datetime(year, 3, 1, 18, 30, tzinfo=timeutil.TZ)
    shown = _answers(end)[-1][0]
    yy = str(year)[2:]
    assert shown.split("\n")[0] == f"✅ Период: 01.02.{yy} 00:00 → 01.03.{yy} 18:30", shown
    assert shown.split("\n")[2].startswith("✏️ Ксюша — изменить"), shown


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
    assert text == "➕ Новый профиль — как назвать?" and labels == ["✖️ Отмена"], (text, labels)

    name = _amsg(fake_bot, "Ксюша")
    await ah.add_client_name(name, services, st)
    text, markup = _answers(name)[-1]
    assert text == "➕ Ксюша — сколько устройств?", text
    assert _labels(markup) == ["1", "2", "3", "5", "10", "∞", "✏️ Другое", "✖️ Отмена"], _labels(markup)

    cb2, nav2 = _acb(fake_bot)
    await ah.add_client_devs_preset(cb2, PresetCB(kind="new_devs", val=3), services, st)
    text, labels = last_screen(nav2)
    assert text.startswith("➕ Ксюша · 3 устройства — трафик в месяц?"), text
    assert labels == ["50 ГБ", "100 ГБ", "200 ГБ", "500 ГБ", "∞", "✏️ Другое", "✖️ Отмена"], labels

    cb3, nav3 = _acb(fake_bot)
    await ah.add_client_traffic_preset(cb3, PresetCB(kind="new_traffic", val=100), services, st)
    text, labels = last_screen(nav3)
    assert text == "➕ Ксюша · 3 устройства · 100 ГБ — срок подписки?", text
    assert labels == ["День", "Неделя", "Месяц", "Год", "∞ Бессрочно", "✖️ Отмена"], labels

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
    assert _answers(seven)[-1][0].startswith("➕ Петя · 7 устройств — трафик в месяц?")

    cb2, nav2 = _acb(fake_bot)
    await ah.add_client_traffic_preset(cb2, PresetCB(kind="new_traffic", val=-1), services, st)
    assert last_screen(nav2)[0] == texts.OTHER_NUMBER_PROMPT
    t = _amsg(fake_bot, "70")
    await ah.add_client_traffic(t, services, st)
    text, markup = _answers(t)[-1]
    assert text == "➕ Петя · 7 устройств · 70 ГБ — срок подписки?", text
    assert "∞ Бессрочно" in _labels(markup)
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
    assert listing[0].startswith("👥 Профили · 0"), listing
    assert Menu(action="main").pack() in [b.callback_data for r in listing[1].inline_keyboard for b in r]
