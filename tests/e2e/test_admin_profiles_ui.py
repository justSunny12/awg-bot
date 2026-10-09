"""E2E: профили глазами администратора — карточка (условные ряды, устройства
в десяти кнопках), «✏️ Изменить» с возвратом после ввода и итогом первой
строкой, период по новому формату, новый профиль пресетами с приглашением
«📤 / 📋», новое приглашение.

Цена ошибки: после ввода — главная вместо «✏️ Изменить», и следующее поле
админ ищет заново; итог не первой строкой — не видно, применилось ли;
приглашение без «📤 / 📋» — ссылку переписывают руками с опечатками;
«✏️ Другое» не возвращает на шаг — профиль с нестандартным лимитом не
создать.

Карточки (лимиты, блок против исчерпанного трафика, истёкшая, пауза со
свёрнутыми устройствами, переезд), «✏️ Изменить», приглашения, переспросы,
пресеты, перенос и блок устройства сверяет эталон (adm.cl*, adm.new*,
adm.dev*); здесь — запись в БД, уборка диалога и рассылка держателям.
"""
import datetime

import pytest

from awgbot.bot import texts
from awgbot.bot.callbacks import CancelCB, ClientCB, PeriodCB, PresetCB
from awgbot.bot.handlers import admin as ah
from awgbot.bot.handlers import reply_commands as rc
from awgbot.core import config
from awgbot.util import timeutil
from tests.conftest import FakeCallback, FakeMessage, FakeState

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


# ── «✏️ Изменить» ─────────────────────────────────────────────────────────────

async def test_rename_refuses_an_empty_name_and_cleans_up_the_dialog(services, fake_bot, make_active_client):
    """Пустое имя — переспрос, ничего не записано; после ввода имя в БД,
    приглашение, пустой и удачный ввод убраны из чата, диалог закрыт.
    Приглашение, переспрос и итог первой строкой «✏️ Изменить» — снимки
    adm.cl.name, adm.cl.name.empty, adm.cl.name.done."""
    c = make_active_client("Ксюша", tg_id=4203)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    services.db.nav_touch(ADMIN, nav.message_id)
    await ah.edit_name_start(cb, ClientCB(action="edit_name", client_id=c.id), services, st)
    empty = _amsg(fake_bot, "  ")
    await ah.edit_name_apply(empty, services, st)
    assert services.db.get_client(c.id).name == "Ксюша", "пустое имя записано"
    msg = _amsg(fake_bot, "Ксения")
    await ah.edit_name_apply(msg, services, st)
    assert services.db.get_client(c.id).name == "Ксения", "имя не записано"
    deleted = {r[2] for r in fake_bot.records if r[0] == "delete_message"}
    assert {nav.message_id, empty.message_id, msg.message_id} <= deleted, "приглашение или ввод остались"
    assert await st.get_data() == {}, "диалог не закрыт"


async def test_cancel_from_an_edit_prompt_goes_back_to_edit(services, fake_bot, make_active_client):
    """«✖️ Отмена» под вводом имени (экран — снимок adm.cl.name.cancel): имя
    не тронуто, диалог закрыт — следующее сообщение не переименует профиль."""
    c = make_active_client("Ксюша", tg_id=4204)
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await ah.edit_name_start(cb, ClientCB(action="edit_name", client_id=c.id), services, st)
    cancel = [s for s in nav.sent if s[0] == "edit_text"][-1][2].inline_keyboard[0][0].callback_data
    cb2, _ = _acb(fake_bot)
    await rc.on_cancel_inline(cb2, CancelCB.unpack(cancel), st, services, role="admin")
    assert services.db.get_client(c.id).name == "Ксюша" and await st.get_data() == {}, \
        "отмена не закрыла диалог или тронула имя"


async def test_period_edit_parses_dates_and_asks_again_on_garbage(services, fake_bot, make_active_client):
    """«✏️ Период»: мусор вместо даты — переспрос; дата с временем и без —
    обе разбираются, период записан минута в минуту. Приглашения, переспрос
    и итог — снимки adm.cl.period, adm.cl.period.bad, adm.cl.period.start,
    adm.cl.period.done."""
    c = make_active_client("Ксюша", tg_id=4205, period_kind="month")
    st = FakeState()
    cb, _ = _acb(fake_bot)
    await ah.edit_period_start(cb, ClientCB(action="edit_period", client_id=c.id), services, st)
    bad = _amsg(fake_bot, "вчера")
    await ah.edit_period_start_apply(bad, services, st)
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
    доходят до диалога и ведут дальше по шагам. Приглашение ввести число,
    переспрос и следующий шаг — снимки adm.new.devs.ask, adm.new.devs.bad,
    adm.new.devs.other, adm.new.period."""
    services.ensure_admin_client()
    st = FakeState()
    await ah.add_client_name(_amsg(fake_bot, "Петя"), services, st)
    cb, _ = _acb(fake_bot)
    await ah.add_client_devs_preset(cb, PresetCB(kind="new_devs", val=-1), services, st)
    bad = _amsg(fake_bot, "семь")
    await ah.add_client_limit(bad, services, st)
    await ah.add_client_limit(_amsg(fake_bot, "7"), services, st)
    cb2, _ = _acb(fake_bot)
    await ah.add_client_traffic_preset(cb2, PresetCB(kind="new_traffic", val=-1), services, st)
    await ah.add_client_traffic(_amsg(fake_bot, "70"), services, st)
    data = await st.get_data()
    assert (data["limit"], data["traffic_gb"]) == (7, 70)


async def test_new_profile_with_a_stale_dialog_does_not_create_anything(services, fake_bot):
    """Срок нажат без диалога — профиль не создан (всплывашка и главная —
    снимок adm.new.stale)."""
    services.ensure_admin_client()
    before = len(services.db.list_clients(include_service=False))
    cb, nav = _acb(fake_bot)
    await ah.add_client_period(cb, PeriodCB(kind="month", ctx="create"), services, FakeState())
    assert len(services.db.list_clients(include_service=False)) == before, "профиль создан из устаревшего диалога"


# ── у профиля админа карточки нет ────────────────────────────────────────────

async def test_admin_profile_has_no_card_and_its_name_is_not_a_link(services, fake_bot):
    """Над собой админ не продлевает, не блокирует и лимитов не ставит —
    карточка была набором запрещённых кнопок. Открытие по кнопке и по ссылке
    cl-<id> ведёт на главную (снимки adm.cl.admin, adm.link.cl.admin), имя
    профиля админа в текстах — не ссылка."""
    from awgbot.bot.handlers.admin.clients import client_card_parts
    services.bot_username = "awg_test_bot"
    services.ensure_admin_client()
    ac = services.admin_client()
    assert await client_card_parts(services, ac.id) is None, "карточка профиля админа собирается"
    assert texts.profile_link(ac, "awg_test_bot") == ac.name
    assert services.cl_link(ac) == ac.name, "ссылка на несуществующую карточку админа"


# ── удаление профиля ─────────────────────────────────────────────────────────

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
