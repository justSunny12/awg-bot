"""Ссылки из экранов в сторону шлюза — то, что не сводится к одному экрану
эталона: deep-link «/start gw-<слот>» встаёт на место живого меню и не роняет
апдейт на мусорных номерах, пометка «карточка с главной» живёт до возврата
на главную и только в своём чате, getMe сразу после ввода токена, подпись
файла на входе функции (пустой бот, экранирование).

Экраны — в эталоне tests/screens/admin.txt: строка «Бот шлюза» в карточках
слота и устройства с ботом, без него, после смены токена и из снимка канала
(adm.gw.card.bot*, adm.gw.card.nobot, adm.gw.card.token_replaced,
adm.gw.card.snapbot*, adm.gw.dev.nobot); карточка по ссылке и «Назад» на
главную после пинга, галочки, ввода и отмены подписи (adm.link.gw*); подпись
под файлом с подписью слота, по username, после смены токена и из снимка
(adm.gw.bundle.*)."""
from __future__ import annotations

import types

import pytest

from awgbot.bot import texts
from awgbot.bot.callbacks import GwMarkCB, GwSlotCB, Menu
from awgbot.bot.handlers import admin as ah
from awgbot.bot.handlers import settings as sh
from awgbot.core import config
from awgbot.runtime import gwbotme
from tests.conftest import FakeCallback, FakeMessage, FakeState
from tests.e2e import test_gateway_slots_ui as _slots_ui
from tests.e2e.test_gateway_slots_ui import _acb, _amsg, _screen, _slot1, _slot2

pytestmark = pytest.mark.e2e
# два слота, заглушки линка и бандла, токены в памяти — та же сцена, что у
# экранов слотов
slots = _slots_ui.slots
ADMIN = config.ADMIN_ID
AGENT = 'Бот шлюза: <a href="https://t.me/pi2_gw_bot">Шлюз &lt;Pi2&gt;</a>'
TOKEN2 = "222222222:BB-second-token-value-long-enough"


@pytest.fixture(autouse=True)
def _no_retry_pause(monkeypatch):
    """Пауза после неудачного getMe живёт на уровне модуля — у каждого теста
    своя, чистая."""
    monkeypatch.setattr(gwbotme, "_next_try", {})


@pytest.fixture(autouse=True)
def _fresh_card_home(monkeypatch):
    """Пометка «карточку открыли с главной» живёт на уровне модуля и по чату
    админа — у каждого теста своя, иначе тесты видели бы чужие пометки."""
    from awgbot.bot.handlers import common as _common
    monkeypatch.setattr(_common, "_card_home", set())


def _cmd(args):
    from aiogram.filters import CommandObject
    return CommandObject(prefix="/", command="start", args=args)


def _ends_with_agent(text: str, agent: str = AGENT) -> bool:
    lines = text.split("\n")
    if lines[-1].startswith("<blockquote"):
        lines = lines[:-1]
    return lines[-1] == agent


async def _card_text(services, fake_bot, slot):
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=slot), services, FakeState())
    return _screen(nav)


# ── строка РФ-доступа: ссылки ведут в существующие карточки ─────────────────

async def test_admin_line_links_point_to_real_slots(services, slots):
    """Номера слотов в ссылках шапки берутся из routing_admin_status: после
    переключения на слот 2 активным числится слот 2 — имя активного ведёт в
    его карточку, а не в карточку резерва."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.db.set_state("routing_gw_2_up_streak", "3")
    info = services.routing_admin_status()
    assert info["active_slot"] == 1 and [s["slot"] for s in info["standby"]] == [2]
    services.gateway_switch(2, manual=True)
    info = services.routing_admin_status()
    assert info["active_slot"] == 2 and [s["slot"] for s in info["standby"]] == [1], info


# ── deep-link «/start gw-<слот>» ─────────────────────────────────────────────

async def test_start_gw_opens_the_slot_card_in_place_of_the_menu(services, slots, fake_bot):
    """Клик по имени шлюза в шапке: карточка слота встаёт на место активного
    меню (правкой, а не новым сообщением), служебная команда из чата убрана.
    Сама карточка по ссылке — эталоны adm.link.gw.nav и adm.link.gw.standby."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.db.set_nav_message_id(ADMIN, 777)
    msg = _amsg(fake_bot, "/start gw-2")
    await ah.admin_start(msg, services, FakeState(), command=_cmd("gw-2"))
    assert msg.deleted, "команда /start gw-2 осталась в чате"
    edits = [r for r in fake_bot.records if r[0] == "edit_message_text"]
    assert edits, "карточка не встала на место меню"
    assert not [s for s in msg.sent if s[0] == "answer"], "карточка ушла новым сообщением при живом меню"


async def test_start_gw_for_a_missing_slot_does_not_break(services, slots, fake_bot):
    """Слот сняли, а старая шапка со ссылкой осталась в чате: клик не роняет
    апдейт, команда убрана, экран показан — и для номера вне слотов, и для
    «gw-0» поверх уже показанного экрана. Экран «слот снят» — эталоны
    adm.link.gw.missing и adm.link.gw.zero."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    for payload in ("gw-9", "gw-0"):
        # первый экран уходит новым сообщением и становится активным меню —
        # второй встаёт на его место правкой; смотрим, что показано последним
        fake_bot.records.clear()
        msg = _amsg(fake_bot, f"/start {payload}")
        await ah.admin_start(msg, services, FakeState(), command=_cmd(payload))
        assert msg.deleted, payload
        shown = [r[2] for r in fake_bot.records if r[0] in ("answer", "edit_message_text")]
        assert shown, f"{payload}: ничего не показано"


# ── «Назад» с карточки, открытой ссылкой с главного экрана ──────────────────

HOME = Menu(action="main").pack()
LIST = GwSlotCB(action="list").pack()


def _back(markup):
    """callback_data кнопки «⬅️ Назад» — последней в карточке."""
    btn = markup.inline_keyboard[-1][0]
    assert btn.text == "⬅️ Назад", [b.text for row in markup.inline_keyboard for b in row]
    return btn.callback_data


def _last_markup(msg, kinds=("answer", "edit_text")):
    return next(s[2] for s in reversed(msg.sent) if s[0] in kinds)


async def _deeplink(services, fake_bot, slot=2):
    """«/start gw-<слот>» без живого меню: карточка уходит сообщением, и её
    клавиатура видна целиком."""
    msg = _amsg(fake_bot, f"/start gw-{slot}")
    await ah.admin_start(msg, services, FakeState(), command=_cmd(f"gw-{slot}"))
    return _last_markup(msg, ("answer",))


def _cb_in(fake_bot, chat_id=ADMIN):
    nav = FakeMessage(chat_id=chat_id, user_id=ADMIN, bot=fake_bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=fake_bot), nav


async def test_card_opened_from_the_list_goes_back_to_the_list(services, slots, fake_bot):
    """После ссылки человек вернулся на главную и открыл ту же карточку из
    списка — «Назад» снова в список, и последующие перерисовки (пинг) пометку
    не возвращают. Пометка «с главной» живёт до возврата на главную: любой
    другой путь в карточку начинается оттуда."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    await _deeplink(services, fake_bot)
    cb, _nav = _cb_in(fake_bot)
    from awgbot.bot.handlers.admin import panel as _panel
    await _panel.admin_main_menu(cb, services, FakeState())
    cb, nav = _cb_in(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=2), services, FakeState())
    assert _back(_last_markup(nav, ("edit_text",))) == LIST, "открыли из списка, а «Назад» всё ещё на главную"
    cb, nav = _cb_in(fake_bot)
    await sh.gw_slot_ping(cb, GwSlotCB(action="ping", slot=2), services)
    assert _back(_last_markup(nav, ("edit_text",))) == LIST, "пометка «с главной» вернулась после пинга"


async def test_card_home_exit_is_per_chat(services, slots, fake_bot):
    """Ссылку открыли в одном чате — в другом карточка ведёт себя как обычно:
    пометка не должна утекать между чатами."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    await _deeplink(services, fake_bot)
    cb, nav = _cb_in(fake_bot, chat_id=ADMIN + 1)
    await sh.gw_slot_ping(cb, GwSlotCB(action="ping", slot=2), services)
    assert _back(_last_markup(nav, ("edit_text",))) == LIST, "пометка чата админа сработала в чужом чате"
    cb, nav = _cb_in(fake_bot)
    await sh.gw_slot_ping(cb, GwSlotCB(action="ping", slot=2), services)
    assert _back(_last_markup(nav, ("edit_text",))) == HOME, "чужой чат снял пометку у чата админа"


# ── getMe сразу после ввода токена ───────────────────────────────────────────

async def _enter_second_token(services, fake_bot):
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gateway_new_yes(cb, GwMarkCB(action="new_yes", slot=0), services, st)
    msg = _amsg(fake_bot, TOKEN2)
    await sh.gateway_token_received(msg, st, services)
    return msg


async def test_token_input_asks_telegram_for_that_slot(services, slots, fake_bot, monkeypatch):
    """Токен второго слота — getMe по нему и для слота 2, уже сохранённым:
    карточка нового слота знает бота с первого показа."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    services.token[1] = "111111111:AA-first-token-value-long-enough"
    asked = []

    async def _refresh(svc, slot):
        asked.append((slot, svc.gw_bot_token(slot)))
        return True
    monkeypatch.setattr(gwbotme, "refresh", _refresh)
    await _enter_second_token(services, fake_bot)
    assert asked == [(2, TOKEN2)], "getMe не для того слота или до сохранения токена"


async def test_token_input_fills_the_card_link_end_to_end(services, slots, fake_bot, monkeypatch):
    """Telegram ответил — ссылка на бота уже в карточке нового слота."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    import aiogram
    monkeypatch.setattr(aiogram, "Bot", lambda token, *a, **k: types.SimpleNamespace(
        get_me=_async(types.SimpleNamespace(username="pi2_gw_bot", first_name="Шлюз <Pi2>")),
        session=types.SimpleNamespace(close=_async(None))))
    await _enter_second_token(services, fake_bot)
    text, _ = await _card_text(services, fake_bot, 2)
    assert _ends_with_agent(text), text


async def test_getme_failure_does_not_stop_the_bundle(services, slots, fake_bot):
    """Telegram не ответил (сеть в тестах закрыта — как у ВПС без выхода в
    api.telegram.org): файл первого применения и инструкция всё равно
    приходят, в карточке просто нет ссылки."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    msg = await _enter_second_token(services, fake_bot)
    assert [g.id for g in services.db.gateways()] == [1, 2]
    docs = [s for s in msg.sent if s[0] == "document"]
    assert len(docs) == 1 and docs[0][1].startswith("🛰 Файл конфигурации шлюза\n"), \
        f"бандл не выдан после отказа getMe: {docs}"
    assert any(s[0] == "answer" and "--install" in s[1] for s in msg.sent), "инструкции нет"
    text, _ = await _card_text(services, fake_bot, 2)
    assert "Бот шлюза" not in text


# ── подпись под файлом конфигурации ──────────────────────────────────────────

def test_bundle_caption_without_known_bot_has_no_link():
    """getMe ещё не отвечал — без ссылки и без пустых скобок."""
    for bot in ({}, None, {"username": "", "name": "x"}):
        got = texts.gateway_bundle_caption("Pi2", bot)
        assert got == ("📤 <b>Конфигурация шлюза Pi2</b>\nПерешли это сообщение боту шлюза — "
                       "он проверит и применит сам\n" "\nℹ️ Возврат в меню удалит это сообщение"), (bot, got)


def test_bundle_caption_escapes_names():
    """Имя устройства, подпись слота и имя бота задаёт человек: `<` или `&`
    без экранирования Telegram отвергает, и файл с ключами не уходит вовсе."""
    got = texts.gateway_bundle_caption("A<B> (x & y)", {"username": "b_bot", "name": "Шлюз <Pi2> & co"})
    assert "A&lt;B&gt; (x &amp; y)" in got, got
    assert ">Шлюз &lt;Pi2&gt; &amp; co</a>" in got, got
    assert "<B>" not in got and "<Pi2>" not in got, got


async def test_send_gw_bundle_failure_sends_no_file(services, slots, fake_bot, monkeypatch):
    """Бандл не собрался — ни документа, ни подписи, только причина."""
    from awgbot.domain.services import ServiceError
    _, pi, _ = slots
    _slot1(services, pi)

    def _fail(slot=None):
        raise ServiceError("нет ключей")
    monkeypatch.setattr(services, "gw_bundle_encrypted", _fail)
    msg = _amsg(fake_bot)
    assert await sh.send_gw_bundle(msg, services, 1) is False
    assert not [s for s in msg.sent if s[0] == "document"]
    assert any(s[0] == "answer" and "нет ключей" in s[1] for s in msg.sent), msg.sent


def _async(value):
    async def _f(*a, **k):
        return value
    return _f


# ── кнопки «Токен бота шлюза» больше нет ─────────────────────────────────────

def _all_routers():
    from awgbot.bot import paging
    from awgbot.bot.handlers import (admin, client, friend, gateway, guide, reply_commands,
                                     routing, settings)
    todo = [m.router for m in (paging, admin, settings, reply_commands, client, friend,
                               guide, routing, gateway)]
    seen = []
    while todo:
        r = todo.pop(0)
        if r not in seen:
            seen.append(r)
            todo.extend(r.sub_routers)
    return seen


def test_nobody_handles_the_removed_token_callback():
    """Старая кнопка могла остаться в чате в прежнем меню: колбэк «token»
    не должен открыть ввод токена ни в одном роутере. Контроль сверки —
    соседнее действие того же колбэка находится."""
    from tests.e2e.test_settings_layout import _first_matching_handler
    routers = _all_routers()
    assert any(_first_matching_handler(r, GwSlotCB(action="bundle", slot=2)) == "gw_slot_bundle"
               for r in routers), "сверка не находит даже живой хендлер — проверка ничего не значит"
    hit = [(r.name, _first_matching_handler(r, GwSlotCB(action="token", slot=2))) for r in routers]
    assert not [h for h in hit if h[1]], f"колбэк снятой кнопки кто-то обрабатывает: {hit}"
    assert not hasattr(sh, "gw_slot_token") and not hasattr(texts, "gateway_token_only_ask"), \
        "хвосты снятой кнопки остались в модулях"
