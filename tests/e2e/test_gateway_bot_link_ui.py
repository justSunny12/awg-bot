"""Ссылки из экранов в сторону шлюза: строка «Бот шлюза» последней в
карточке слота и в карточке устройства-шлюза, deep-link «/start gw-<слот>»
из строки РФ-доступа в шапке админа (и «Назад» с такой карточки — на
главную), getMe сразу после ввода токена и подпись под файлом конфигурации
со ссылкой на бота шлюза."""
from __future__ import annotations

import types

import pytest

from awgbot.bot import texts
from awgbot.bot.callbacks import DeviceCB, GwMarkCB, GwSlotCB, Menu, SetCB
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


def _known_bot(services, slot=2):
    """Бот слота уже известен: токен есть, ответ getMe в кэше."""
    services.token[slot] = TOKEN2
    services.set_gw_bot_identity(slot, "pi2_gw_bot", "Шлюз <Pi2>")


async def _card_text(services, fake_bot, slot):
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_card(cb, GwSlotCB(action="card", slot=slot), services, FakeState())
    return _screen(nav)


# ── «Бот шлюза» в карточках ──────────────────────────────────────────────────

async def test_slot_card_ends_with_the_agent_bot_link(services, slots, fake_bot):
    """Строка — последней, после пустой строки, и больше ничего в карточке не
    меняет: ни текста выше, ни кнопок."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    plain, plain_labels = await _card_text(services, fake_bot, 2)
    assert "Бот шлюза" not in plain, "без ответа getMe строки быть не должно"
    _known_bot(services, 2)
    text, labels = await _card_text(services, fake_bot, 2)
    assert text == plain + "\n\n" + AGENT, "строка «Бот шлюза» не последней или карточка изменилась выше"
    assert labels == plain_labels, "кнопки карточки от бота шлюза меняться не должны"
    other, _ = await _card_text(services, fake_bot, 1)
    assert "Бот шлюза" not in other, "бот слота 2 попал в карточку слота 1"


async def test_slot_card_forgets_the_bot_when_its_token_is_replaced(services, slots, fake_bot):
    """Токен слота сменили — прежний бот уже не тот: ссылка из карточки
    пропадает до нового getMe."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _known_bot(services, 2)
    services.token[2] = "222222222:BB-another-token-value-long-enough"
    text, _ = await _card_text(services, fake_bot, 2)
    assert "Бот шлюза" not in text, "ссылка на бота старого токена пережила замену токена"


async def test_gateway_device_card_ends_with_the_agent_bot_link(services, slots, fake_bot):
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)

    async def _open():
        cb, nav = _acb(fake_bot)
        await ah.admin_device_open(cb, DeviceCB(action="open", device_id=pi2.id), services)
        return _screen(nav)
    plain, plain_labels = await _open()
    assert "Бот шлюза" not in plain
    _known_bot(services, 2)
    text, labels = await _open()
    assert text == plain + "\n\n" + AGENT, "в карточке устройства строка не последней или сдвинула остальное"
    assert labels == plain_labels


# ── строка РФ-доступа: ссылки ведут в существующие карточки ─────────────────

async def test_admin_line_links_point_to_real_slots(services, slots):
    """Номера слотов в ссылках берутся из routing_admin_status: после
    переключения на слот 2 имя активного ведёт в карточку слота 2."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.db.set_state("routing_gw_2_up_streak", "3")
    info = services.routing_admin_status()
    assert info["active_slot"] == 1 and [s["slot"] for s in info["standby"]] == [2]
    line = texts.routing_admin_status_line(info, "awg_test_bot")
    assert '<a href="https://t.me/awg_test_bot?start=gw-1">NASPi</a>' in line, line
    assert '<a href="https://t.me/awg_test_bot?start=gw-2">резерв</a> жив' in line, line
    services.gateway_switch(2, manual=True)
    info = services.routing_admin_status()
    assert info["active_slot"] == 2 and [s["slot"] for s in info["standby"]] == [1], info


# ── deep-link «/start gw-<слот>» ─────────────────────────────────────────────

async def test_start_gw_opens_the_slot_card_in_place_of_the_menu(services, slots, fake_bot):
    """Клик по имени шлюза в шапке: карточка слота встаёт на место активного
    меню, служебная команда из чата убрана."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _known_bot(services, 2)
    services.db.set_nav_message_id(ADMIN, 777)
    msg = _amsg(fake_bot, "/start gw-2")
    await ah.admin_start(msg, services, FakeState(), command=_cmd("gw-2"))
    assert msg.deleted, "команда /start gw-2 осталась в чате"
    edits = [r for r in fake_bot.records if r[0] == "edit_message_text"]
    assert edits, "карточка не встала на место меню"
    shown = edits[-1][2]
    assert shown.startswith("🛰 <b>Шлюз «Pi2»</b>") and shown.endswith("\n\n" + AGENT), shown
    assert not [s for s in msg.sent if s[0] == "answer"], "карточка ушла новым сообщением при живом меню"


async def test_start_gw_card_does_not_call_a_live_gateway_dead(services, slots, fake_bot):
    """По ссылке пинг не меряется (lazy_ping=False), а пустой замер карточка
    печатает как «шлюз не отвечает»: живой резерв, у которого пинг просто ещё
    не мерили, по клику из шапки выглядит мёртвым. По кнопке «Карточка» тот
    же слот показывает честные миллисекунды."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.db.set_nav_message_id(ADMIN, 777)
    msg = _amsg(fake_bot, "/start gw-2")
    await ah.admin_start(msg, services, FakeState(), command=_cmd("gw-2"))
    shown = [r for r in fake_bot.records if r[0] == "edit_message_text"][-1][2]
    assert "шлюз не отвечает" not in shown, \
        "карточка по ссылке объявила неизмеренный пинг отказом шлюза"


async def test_start_gw_without_an_active_menu_sends_the_card(services, slots, fake_bot):
    _, pi, pi2 = slots
    _slot1(services, pi)
    msg = _amsg(fake_bot, "/start gw-1")
    await ah.admin_start(msg, services, FakeState(), command=_cmd("gw-1"))
    sent = [s[1] for s in msg.sent if s[0] == "answer"]
    assert sent and "Шлюз «NASPi»" in sent[-1], sent


async def test_start_gw_for_a_missing_slot_does_not_break(services, slots, fake_bot):
    """Слот сняли, а старая шапка со ссылкой осталась в чате: клик не роняет
    апдейт — свой экран «слот снят» с «Назад» в раздел маршрутизации (не
    «профиль не найден» с кнопками потребления), команда убрана."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    msgs = []
    for payload in ("gw-9", "gw-0"):
        # первый экран уходит новым сообщением и становится активным меню —
        # второй встаёт на его место правкой; смотрим, что показано последним
        fake_bot.records.clear()
        msg = _amsg(fake_bot, f"/start {payload}")
        await ah.admin_start(msg, services, FakeState(), command=_cmd(payload))
        msgs.append(msg)
        assert msg.deleted, payload
        shown = [r[2] for r in fake_bot.records if r[0] in ("answer", "edit_message_text")]
        assert shown and shown[-1] == "🛰 Такого шлюза больше нет — слот снят", (payload, shown)
    # у первого показа (сообщением) видна клавиатура
    markup = next(s[2] for s in msgs[0].sent if s[0] == "answer")
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert datas == [SetCB(sec="rt").pack()], datas


async def test_start_gw_with_garbage_is_a_plain_start(services, slots, fake_bot):
    """«/start gw-abc» — не ссылка на карточку: обычный /start с панелью, а не
    «не найдено»."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    msg = _amsg(fake_bot, "/start gw-abc")
    await ah.admin_start(msg, services, FakeState(), command=_cmd("gw-abc"))
    shown = [s[1] for s in msg.sent if s[0] == "answer"]
    assert shown, "обычный /start ничего не показал"
    assert "не найден" not in shown[-1] and "Шлюз «" not in shown[-1], shown[-1]
    plain = _amsg(fake_bot, "/start")
    await ah.admin_start(plain, services, FakeState(), command=_cmd(None))
    assert [s[1] for s in plain.sent if s[0] == "answer"][-1] == shown[-1], \
        "«/start gw-abc» показал не то же, что голый /start"


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


async def test_start_gw_card_back_leads_to_the_main_screen(services, slots, fake_bot):
    """Человек кликнул имя шлюза в шапке главного экрана: «Назад» с карточки
    должно вернуть его туда же. Уводить в список шлюзов, где он не был, —
    заблудиться на два экрана вглубь настроек."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    assert _back(await _deeplink(services, fake_bot)) == HOME, "«Назад» с карточки по ссылке ведёт не на главную"


async def test_start_gw_card_keeps_the_home_exit_after_ping(services, slots, fake_bot):
    """«📡 Пинг» перерисовывает карточку — выход на главную не должен
    подмениться списком от одного нажатия."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    await _deeplink(services, fake_bot)
    cb, nav = _cb_in(fake_bot)
    await sh.gw_slot_ping(cb, GwSlotCB(action="ping", slot=2), services)
    markup = _last_markup(nav, ("edit_text",))
    assert _back(markup) == HOME, "после пинга «Назад» перестало вести на главную"


async def test_start_gw_card_keeps_the_home_exit_after_preferred_toggle(services, slots, fake_bot):
    """Галочка предпочтительного — тоже перерисовка карточки по её кнопке."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    await _deeplink(services, fake_bot)
    cb, nav = _cb_in(fake_bot)
    await sh.gw_slot_pref(cb, GwSlotCB(action="pref", slot=2), services)
    assert _back(_last_markup(nav, ("edit_text",))) == HOME, "после галочки «Назад» перестало вести на главную"


async def test_start_gw_card_keeps_the_home_exit_after_label_input(services, slots, fake_bot):
    """Подпись введена — карточка приходит заново сообщением, выход тот же."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    await _deeplink(services, fake_bot)
    st = FakeState()
    cb, _ = _cb_in(fake_bot)
    await sh.gw_slot_label(cb, GwSlotCB(action="label", slot=2), services, st)
    msg = _amsg(fake_bot, "дача")
    await sh.gateway_label_received(msg, st, services)
    assert _back(_last_markup(msg, ("answer",))) == HOME, "после ввода подписи «Назад» перестало вести на главную"


async def test_start_gw_card_keeps_the_home_exit_after_label_cancel(services, slots, fake_bot):
    """Открыл подпись и передумал — «✖️ Отмена» возвращает в ту же карточку;
    выход с неё должен остаться на главную, как после ввода подписи. Отмена
    шлёт тот же GwSlotCB(card), что и кнопка списка, — если пометка
    снимается и тут, человек, пришедший с главной, уходит «Назад» в список."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    await _deeplink(services, fake_bot)
    st = FakeState()
    cb, nav = _cb_in(fake_bot)
    await sh.gw_slot_label(cb, GwSlotCB(action="label", slot=2), services, st)
    cancel = _last_markup(nav, ("edit_text", "answer")).inline_keyboard[0][0]
    assert cancel.text == "✖️ Отмена"
    cb2, nav2 = _cb_in(fake_bot)
    await sh.gw_slot_card(cb2, GwSlotCB.unpack(cancel.callback_data), services, st)
    assert _back(_last_markup(nav2, ("edit_text",))) == HOME, \
        "отмена ввода подписи увела выход карточки с главной в список"


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


async def test_start_gw_single_slot_card_also_goes_home(services, slots, fake_bot):
    """С одним шлюзом обычный «Назад» ведёт в раздел маршрутизации; по
    ссылке — всё равно на главную."""
    _, pi, _ = slots
    _slot1(services, pi)
    assert _back(await _deeplink(services, fake_bot, 1)) == HOME


def test_card_keyboard_back_targets():
    """Сама клавиатура: back_home главнее списка, без него — как раньше."""
    from awgbot.bot import keyboards as kb
    gw = types.SimpleNamespace(id=2, device_id=0, preferred=0, lan_mode=0, label="")
    st = {"gateway": gw, "states": [{}, {}], "active": False, "preferred": False}
    assert _back(kb.gateway_card(st, back_to_list=True, back_home=True)) == HOME
    assert _back(kb.gateway_card(st, back_to_list=True)) == LIST
    assert _back(kb.gateway_card(st, back_to_list=False)) == SetCB(sec="rt").pack()


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
    assert text.endswith("\n\n" + AGENT), text


async def test_getme_failure_does_not_stop_the_bundle(services, slots, fake_bot):
    """Telegram не ответил (сеть в тестах закрыта — как у ВПС без выхода в
    api.telegram.org): файл первого применения и инструкция всё равно
    приходят, в карточке просто нет ссылки."""
    _, pi, pi2 = slots
    _slot1(services, pi)
    msg = await _enter_second_token(services, fake_bot)
    assert [g.id for g in services.db.gateways()] == [1, 2]
    docs = [s for s in msg.sent if s[0] == "document"]
    assert len(docs) == 1 and "первого применения" in docs[0][1], "бандл не выдан после отказа getMe"
    assert any(s[0] == "answer" and "--install" in s[1] for s in msg.sent), "инструкции нет"
    text, _ = await _card_text(services, fake_bot, 2)
    assert "Бот шлюза" not in text


# ── подпись под файлом конфигурации ──────────────────────────────────────────

def test_bundle_caption_links_the_agent_bot():
    """Файл уходит в чат ВПС, а применять его надо в другом боте: подпись
    говорит, для какого шлюза файл и куда его переслать — ссылкой."""
    got = texts.gateway_bundle_caption("«Pi2» (дом 2)", {"username": "pi2_gw_bot", "name": "Шлюз Pi2"})
    assert got == ('⚙️ Конфигурация шлюза <b>«Pi2» (дом 2)</b>.\nПерешли файл боту шлюза '
                   '(<a href="https://t.me/pi2_gw_bot">Шлюз Pi2</a>) — он проверит и применит сам.'), got


def test_bundle_caption_without_known_bot_has_no_link():
    """getMe ещё не отвечал — без ссылки и без пустых скобок."""
    for bot in ({}, None, {"username": "", "name": "x"}):
        got = texts.gateway_bundle_caption("«Pi2»", bot)
        assert got == "⚙️ Конфигурация шлюза <b>«Pi2»</b>.\nПерешли файл боту шлюза — он проверит и применит сам.", (bot, got)


def test_bundle_caption_without_bot_name_shows_username():
    got = texts.gateway_bundle_caption("«Pi2»", {"username": "pi2_gw_bot"})
    assert '(<a href="https://t.me/pi2_gw_bot">pi2_gw_bot</a>)' in got, got


def test_bundle_caption_escapes_names():
    """Имя устройства, подпись слота и имя бота задаёт человек: `<` или `&`
    без экранирования Telegram отвергает, и файл с ключами не уходит вовсе."""
    got = texts.gateway_bundle_caption("«A<B>» (x & y)", {"username": "b_bot", "name": "Шлюз <Pi2> & co"})
    assert "«A&lt;B&gt;» (x &amp; y)" in got, got
    assert ">Шлюз &lt;Pi2&gt; &amp; co</a>" in got, got
    assert "<B>" not in got and "<Pi2>" not in got, got


async def test_send_gw_bundle_captions_the_file_with_slot_and_bot(services, slots, fake_bot):
    """Документ реально уходит с подписью: имя и подпись слота, ссылка на его
    бота; соседний слот без известного бота — без ссылки."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.db.gateway_update(2, label="дом <2>")
    _known_bot(services, 2)
    msg = _amsg(fake_bot)
    assert await sh.send_gw_bundle(msg, services, 2) is True
    docs = [s[1] for s in msg.sent if s[0] == "document"]
    assert docs == ['⚙️ Конфигурация шлюза <b>«Pi2» (дом &lt;2&gt;)</b>.\nПерешли файл боту шлюза '
                    '(<a href="https://t.me/pi2_gw_bot">Шлюз &lt;Pi2&gt;</a>) — он проверит и применит сам.'], docs
    msg1 = _amsg(fake_bot)
    assert await sh.send_gw_bundle(msg1, services, 1) is True
    docs1 = [s[1] for s in msg1.sent if s[0] == "document"]
    assert docs1 == ["⚙️ Конфигурация шлюза <b>«NASPi»</b>.\nПерешли файл боту шлюза — он проверит и применит сам."], \
        "бот слота 2 попал в подпись файла слота 1"


async def test_send_gw_bundle_forgets_the_bot_of_a_replaced_token(services, slots, fake_bot):
    """Токен слота сменили — ссылка на прежнего бота в подписи увела бы файл
    с ключами не туда."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _known_bot(services, 2)
    services.token[2] = "222222222:BB-another-token-value-long-enough"
    msg = _amsg(fake_bot)
    await sh.send_gw_bundle(msg, services, 2)
    docs = [s[1] for s in msg.sent if s[0] == "document"]
    assert docs and "t.me/pi2_gw_bot" not in docs[0], docs


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


# ── бот шлюза из снимка канала (токена на сервере нет) ───────────────────────

def _snapshot_bot(services, slot, username, name, rev=1, full=True):
    """Агент прислал себя снимком канала: полным или дельтой."""
    body = {"agent_bot": {"username": username, "name": name}, "rev": rev}
    if full:
        body.update(agent_version="3.1.0", mark_status="confirmed", egress_ok=True)
    assert services.gwlink_snapshot_in(slot, body, rev, full) is True


async def test_slot_card_links_the_bot_from_the_channel_snapshot(services, slots, fake_bot):
    """Слот заведён до того, как сервер стал спрашивать токен, и ввести токен
    в интерфейсе негде: ссылку в чат бота даёт снимок канала — та же строка,
    последней, и кнопки карточки те же."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    assert services.token == {}, "сцена этого теста — слот без токена на сервере"
    _snapshot_bot(services, 2, "pi2_gw_bot", "Шлюз <Pi2>")
    text, labels = await _card_text(services, fake_bot, 2)
    assert text.endswith("\n\n" + AGENT), f"карточка без ссылки на бота из снимка:\n{text}"
    other, _ = await _card_text(services, fake_bot, 1)
    assert "Бот шлюза" not in other, "бот из снимка слота 2 попал в карточку слота 1"


async def test_slot_card_link_follows_the_snapshot_delta(services, slots, fake_bot):
    """Бота переименовали — дельта снимка, и карточка уже с новым именем;
    агент перезапустился без сети (пустой username) — строки нет."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _snapshot_bot(services, 2, "pi2_gw_bot", "Шлюз <Pi2>")
    _snapshot_bot(services, 2, "pi2_gw_bot", "Новое имя", rev=2, full=False)
    text, _ = await _card_text(services, fake_bot, 2)
    assert text.endswith('Бот шлюза: <a href="https://t.me/pi2_gw_bot">Новое имя</a>'), text
    _snapshot_bot(services, 2, "", "", rev=3, full=False)
    text, _ = await _card_text(services, fake_bot, 2)
    assert "Бот шлюза" not in text, "пустой username в снимке, а ссылка осталась"


async def test_token_answer_wins_over_the_snapshot_in_the_card(services, slots, fake_bot):
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _snapshot_bot(services, 2, "snap_bot", "Из снимка")
    _known_bot(services, 2)
    text, _ = await _card_text(services, fake_bot, 2)
    assert text.endswith("\n\n" + AGENT) and "snap_bot" not in text, text


async def test_send_gw_bundle_captions_the_bot_from_the_snapshot(services, slots, fake_bot):
    """Файл с ключами уходит с подписью, какому боту его пересылать, и без
    токена на сервере — по снимку канала."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _snapshot_bot(services, 2, "pi2_gw_bot", "Шлюз <Pi2>")
    msg = _amsg(fake_bot)
    assert await sh.send_gw_bundle(msg, services, 2) is True
    docs = [s[1] for s in msg.sent if s[0] == "document"]
    assert docs == ['⚙️ Конфигурация шлюза <b>«Pi2»</b>.\nПерешли файл боту шлюза '
                    '(<a href="https://t.me/pi2_gw_bot">Шлюз &lt;Pi2&gt;</a>) — он проверит и применит сам.'], docs


# ── кнопки «Токен бота шлюза» больше нет ─────────────────────────────────────

def test_bundle_screen_has_no_token_button():
    """Токен задаётся один раз при настройке; экран выпуска файла — только
    «Выпустить» и «Отмена», для слота и без него."""
    from awgbot.bot import keyboards as kbs
    for slot in (0, 1, 2):
        mk = kbs.settings_routing_bundle(slot)
        labels = [b.text for row in mk.inline_keyboard for b in row]
        assert labels == ["📤 Выпустить файл", "✖️ Отмена"], (slot, labels)
        datas = [b.callback_data for row in mk.inline_keyboard for b in row]
        assert GwSlotCB(action="token", slot=slot).pack() not in datas, (slot, datas)


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
