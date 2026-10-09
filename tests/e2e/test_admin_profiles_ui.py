"""E2E: профили глазами администратора — карточка (условные ряды, устройства
в десяти кнопках), «✏️ Изменить» с возвратом после ввода и итогом первой
строкой, период по новому формату, новый профиль пресетами с приглашением
«📤 / 📋», новое приглашение.

Цена ошибки: после ввода — главная вместо «✏️ Изменить», и следующее поле
админ ищет заново; итог не первой строкой — не видно, применилось ли;
приглашение без «📤 / 📋» — ссылку переписывают руками с опечатками;
«✏️ Другое» не возвращает на шаг — профиль с нестандартным лимитом не
создать.

Карточки, «✏️ Изменить», приглашения и пресеты в снятых состояниях сверяет
эталон (adm.cl*, adm.new*, adm.dev*); здесь — запись в БД, переспросы,
уборка диалога и состояния, которых в снимках нет.
"""
import datetime

import pytest

from awgbot.bot import texts
from awgbot.bot.callbacks import CancelCB, ClientCB, PeriodCB, PresetCB
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


# ── карточка ─────────────────────────────────────────────────────────────────

async def test_card_traffic_line_against_the_profile_limit(services, fake_bot, make_active_client):
    """«📊» карточки при лимите трафика — «из N ГБ» со стрелками: сколько
    осталось, видно без калькулятора. Карточки без лимита — снимки adm.cl*."""
    c = make_active_client("Ксюша", tg_id=4101, period_kind="month", traffic_limit=100 * G)
    d = services.add_device(c.id, "iPhone")
    services.db.add_traffic_bulk([(d.device_id, G, 11 * G)])
    cb, nav = _acb(fake_bot)
    await ah.client_open(cb, ClientCB(action="open", client_id=c.id), services, FakeState())
    text, _ = last_screen(nav)
    assert "📊 12 из 100 ГБ (↑1 ↓11)" in text.split("\n"), text


async def test_card_with_pause_and_rf_still_collapses_devices_and_lists_them_unlimited(
        services, fake_bot, make_active_client):
    """Пауза и РФ-доступ добавляют ряды — устройства всё равно сворачиваются в
    «📱 Устройства: N», карточка не вылезает за десять рядов; у профиля без
    лимита устройств шапка списка — просто число. Свёрнутая карточка и список
    с лимитом — снимки adm.cl.many, adm.cl.devices."""
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


# ── «✏️ Изменить» ─────────────────────────────────────────────────────────────

async def _edit_screen(services, bot, cid):
    cb, nav = _acb(bot)
    await ah.client_edit(cb, ClientCB(action="edit", client_id=cid), services, FakeState())
    return last_screen(nav)


async def test_edit_screen_of_an_unlimited_profile(services, fake_bot, make_active_client):
    """Бессрочный профиль без лимита устройств — «∞ устройств», а не «0».
    Экран с лимитами — снимки adm.cl.edit, adm.cl.period.forever."""
    u = make_active_client("Вечный", tg_id=4202, period_kind="never", device_limit=0)
    text, _ = await _edit_screen(services, fake_bot, u.id)
    assert text == "✏️ <b>Вечный</b> — изменить\nБессрочная · ∞ устройств · ∞ ГБ в месяц", text


async def test_rename_refuses_an_empty_name_and_cleans_up_the_dialog(services, fake_bot, make_active_client):
    """Пустое имя — переспрос, ничего не записано; после ввода имя в БД,
    приглашение, пустой и удачный ввод убраны из чата, диалог закрыт.
    Приглашение и итог первой строкой «✏️ Изменить» — снимки adm.cl.name,
    adm.cl.name.done."""
    c = make_active_client("Ксюша", tg_id=4203)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    services.db.nav_touch(ADMIN, nav.message_id)
    await ah.edit_name_start(cb, ClientCB(action="edit_name", client_id=c.id), services, st)
    empty = _amsg(fake_bot, "  ")
    await ah.edit_name_apply(empty, services, st)
    assert [t for t, _ in _answers(empty)] == [texts.NAME_EMPTY]
    assert services.db.get_client(c.id).name == "Ксюша", "пустое имя записано"
    msg = _amsg(fake_bot, "Ксения")
    await ah.edit_name_apply(msg, services, st)
    assert services.db.get_client(c.id).name == "Ксения"
    deleted = {r[2] for r in fake_bot.records if r[0] == "delete_message"}
    assert {nav.message_id, empty.message_id, msg.message_id} <= deleted, "приглашение или ввод остались"
    assert await st.get_data() == {}


async def test_cancel_from_an_edit_prompt_goes_back_to_edit(services, fake_bot, make_active_client):
    """«✖️ Отмена» под вводом имени (колбэк — снимок adm.cl.name) — обратно в
    «✏️ Изменить», имя не тронуто, диалог закрыт: следующее сообщение не
    переименует профиль."""
    c = make_active_client("Ксюша", tg_id=4204)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.edit_name_start(cb, ClientCB(action="edit_name", client_id=c.id), services, st)
    cancel = [s for s in nav.sent if s[0] == "edit_text"][-1][2].inline_keyboard[0][0].callback_data
    cb2, nav2 = _acb(fake_bot)
    await rc.on_cancel_inline(cb2, CancelCB.unpack(cancel), st, services, role="admin")
    text, _ = last_screen(nav2)
    assert text.startswith("✏️ <b>Ксюша</b> — изменить\n"), text
    assert services.db.get_client(c.id).name == "Ксюша" and await st.get_data() == {}


async def test_period_edit_parses_dates_and_asks_again_on_garbage(services, fake_bot, make_active_client):
    """«✏️ Период»: мусор вместо даты — переспрос; дата с временем и без —
    обе разбираются, период записан минута в минуту. Приглашения и итог —
    снимки adm.cl.period, adm.cl.period.start, adm.cl.period.done."""
    c = make_active_client("Ксюша", tg_id=4205, period_kind="month")
    st = FakeState()
    cb, _ = _acb(fake_bot)
    await ah.edit_period_start(cb, ClientCB(action="edit_period", client_id=c.id), services, st)
    bad = _amsg(fake_bot, "вчера")
    await ah.edit_period_start_apply(bad, services, st)
    assert [t for t, _ in _answers(bad)] == [texts.PERIOD_BAD]
    year = timeutil.now().year + 1
    start = _amsg(fake_bot, f"01.02.{year}")
    await ah.edit_period_start_apply(start, services, st)
    end = _amsg(fake_bot, f"01.03.{year} 18:30")
    await ah.edit_period_end_apply(end, services, st)
    fresh = services.db.get_client(c.id)
    assert timeutil.parse_iso(fresh.period_end) == datetime.datetime(year, 3, 1, 18, 30, tzinfo=timeutil.TZ)
    assert timeutil.parse_iso(fresh.period_start) == datetime.datetime(year, 2, 1, 0, 0, tzinfo=timeutil.TZ), \
        "дата без времени — 00:00"


async def test_period_end_zero_makes_it_unlimited(services, fake_bot, make_active_client):
    c = make_active_client("Ксюша", tg_id=4206, period_kind="month")
    st = FakeState()
    cb, _ = _acb(fake_bot)
    await ah.edit_period_start(cb, ClientCB(action="edit_period", client_id=c.id), services, st)
    await ah.edit_period_start_apply(_amsg(fake_bot, "-"), services, st)
    end = _amsg(fake_bot, "0")
    await ah.edit_period_end_apply(end, services, st)
    assert services.db.get_client(c.id).period_end is None, "«0» у окончания не сделал профиль бессрочным"


# ── новый профиль ────────────────────────────────────────────────────────────

async def test_new_profile_presets_create_exactly_what_was_chosen(services, fake_bot):
    """Имя → три шага пресетами → профиль создан ровно с выбранными
    значениями, приглашение — новым живым меню, диалог закрыт. Шаги и
    приглашение с «📤 / 📋» — снимки adm.new, adm.new.devs, adm.new.traffic,
    adm.new.period, adm.new.invite."""
    services.ensure_admin_client()
    st = FakeState()
    cb, _ = _acb(fake_bot)
    await ah.add_client_start(cb, services, st)
    await ah.add_client_name(_amsg(fake_bot, "Ксюша"), services, st)
    cb2, _ = _acb(fake_bot)
    await ah.add_client_devs_preset(cb2, PresetCB(kind="new_devs", val=3), services, st)
    cb3, _ = _acb(fake_bot)
    await ah.add_client_traffic_preset(cb3, PresetCB(kind="new_traffic", val=100), services, st)
    cb4, nav4 = _acb(fake_bot)
    await ah.add_client_period(cb4, PeriodCB(kind="month", ctx="create"), services, st)
    c = next(x for x in services.db.list_clients(include_service=False) if x.name == "Ксюша")
    assert (c.device_limit, int(c.traffic_limit), c.period_kind) == (3, 100 * G, "month")
    assert _answers(nav4), "приглашение не отправлено"
    assert services.db.get_nav_message_id(ADMIN) not in (None, nav4.message_id), "приглашение — не живое меню"
    assert await st.get_data() == {}


async def test_new_profile_other_number_comes_back_to_the_flow(services, fake_bot):
    """«✏️ Другое» на шаге устройств и трафика: мусор — переспрос, числа
    доходят до диалога и ведут дальше по шагам. Приглашение ввести число и
    следующий шаг — снимки adm.cl.limit.other, adm.new.devs.other,
    adm.new.period."""
    services.ensure_admin_client()
    st = FakeState()
    await ah.add_client_name(_amsg(fake_bot, "Петя"), services, st)
    cb, _ = _acb(fake_bot)
    await ah.add_client_devs_preset(cb, PresetCB(kind="new_devs", val=-1), services, st)
    bad = _amsg(fake_bot, "семь")
    await ah.add_client_limit(bad, services, st)
    assert [t for t, _ in _answers(bad)] == [texts.NUMBER_BAD_LIMIT]
    await ah.add_client_limit(_amsg(fake_bot, "7"), services, st)
    cb2, _ = _acb(fake_bot)
    await ah.add_client_traffic_preset(cb2, PresetCB(kind="new_traffic", val=-1), services, st)
    await ah.add_client_traffic(_amsg(fake_bot, "70"), services, st)
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


async def test_card_expired_subscription_shows_the_date_and_the_access_line(
        services, fake_bot, make_active_client):
    c = make_active_client("Ксюша", tg_id=4405, period_kind="month")
    services.db.update_client_fields(c.id, period_end="2026-09-01T00:00:00+03:00", status="expired")
    lines, _, _ = await _card(services, fake_bot, c.id)
    i = _sub_index(lines)
    assert lines[i - 1] == "🟡 доступ приостановлен", lines
    assert lines[i].startswith("💳 🔴 истекла 01.09"), lines


# без лимита — «📊 0 ГБ (безлимит)», снимок adm.cl.pending
@pytest.mark.parametrize("limit, expected", [(100 * G, "📊 0 из 100 ГБ")])
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


# ── карточка устройства у админа ─────────────────────────────────────────────

# свой лимит без профильного, без лимитов вовсе (с трафиком и без) — снимки
# adm.dev.limit.preset, adm.dev.client, adm.dev.alien
@pytest.mark.parametrize("dev_limit, profile_limit, used, expected", [
    (0, 100, True, "📊 3.2 из 100 ГБ (↑0.4 ↓2.8), лимит профиля"),
    (50, 100, True, "📊 3.2 из 50 ГБ (↑0.4 ↓2.8)"),          # свой лимит важнее профильного
    (50, 0, False, "📊 0 из 50 ГБ"),
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


# ── перенос и блокировка устройства: чьё оно ─────────────────────────────────

def _cl(c):
    return f'<a href="https://t.me/awg_test_bot?start=cl-{c.id}">{c.name}</a>'


async def test_reassign_names_the_device_and_both_profiles(services, fake_bot, make_active_client):
    """Вопрос переноса и итог называют устройство в кавычках и профили
    ссылками: «iPhone» у каждого второго — без владельца админ не отличит,
    чей телефон он переносит и куда тот ушёл. Перенос пира без профиля —
    снимки adm.dev.reassign, adm.dev.reassign.go."""
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


async def test_block_toast_names_the_owner_in_plain_text(services, fake_bot, make_active_client):
    """Всплывашка — простой текст (ссылки в ней не работают); тихая
    блокировка — с «(тихо)», иначе админ не поймёт, узнал ли владелец.
    Блокировка с уведомлением — снимок adm.dev.block.do."""
    from awgbot.bot.callbacks import BlockCB
    services.bot_username = "awg_test_bot"
    petya = make_active_client("Петя", tg_id=4604)
    dc = services.add_device(petya.id, "iPhone")
    cb, _ = _acb(fake_bot)
    await ah.admin_block_do(cb, BlockCB(target="dev", action="block", ref=dc.device_id, kind="silent"),
                            services)
    assert cb.answers[-1][0] == "🛑 Устройство «iPhone» (Петя) заблокировано (тихо)", cb.answers


async def test_deleting_a_profile_with_a_partial_failure_still_tells_the_holders(services, fake_bot,
                                                                                 make_active_client, monkeypatch):
    """Снятие одного пира упало — снятые устройства всё равно сняты, и их
    держатели обязаны узнать: раньше обработчик выходил по отказу до рассылки,
    и у гостей VPN гас без объяснений."""
    from types import SimpleNamespace
    c = make_active_client(tg_id=4190, name="Даритель")
    dev = SimpleNamespace(name="Планшет", id=777)
    monkeypatch.setattr(services, "delete_client_with_devices",
                        lambda cid: {"failed": ["Ноут"], "removed": [(8100, dev)], "n": 1})
    cb, nav = _acb(fake_bot)
    await ah.client_delete_apply(cb, ClientCB(action="delete_yes", client_id=c.id), services)
    assert any(r[0] == "send_message" and r[1] == 8100 for r in fake_bot.records), fake_bot.records
