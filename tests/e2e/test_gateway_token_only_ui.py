"""Токен бота уже настроенного слота: кнопка «🤖 Токен бота шлюза» на экране
«⚙️ Конфигурация шлюза» слота, приглашение к вводу и карточка слота после
ввода. Слот, заведённый до того, как сервер стал спрашивать токен, иначе не
получит ссылку на бота шлюза в карточках, — а ввод токена здесь не должен
касаться ни устройства, ни ключей, ни файла первого применения."""
from __future__ import annotations

import types

import pytest

from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.callbacks import GwSlotCB, SetCB
from awgbot.bot.handlers import settings as sh
from awgbot.core import config
from tests.conftest import FakeState
from tests.e2e import test_gateway_bot_link_ui as _link_ui
from tests.e2e import test_gateway_slots_ui as _slots_ui
from tests.e2e.test_gateway_bot_link_ui import AGENT, HOME, LIST, TOKEN2, _back, _deeplink, _known_bot
from tests.e2e.test_gateway_slots_ui import _acb, _amsg, _labels, _slot1, _slot2

pytestmark = pytest.mark.e2e
# та же сцена, что у экранов слотов: два слота, токены в памяти, линк подменён
slots = _slots_ui.slots
# пауза getMe и пометка «карточка с главной» — на уровне модуля, у каждого теста свои
_no_retry_pause = _link_ui._no_retry_pause
_fresh_card_home = _link_ui._fresh_card_home

ADMIN = config.ADMIN_ID
TOKEN_BTN = "🤖 Токен бота шлюза"
WARN = "⚠️ Telegram не ответил по этому токену"


def _async(value=None, exc=None):
    async def _f(*a, **k):
        if exc is not None:
            raise exc
        return value
    return _f


def _telegram(monkeypatch, *, username="pi2_gw_bot", first_name="Шлюз <Pi2>", fail=False):
    """Подмена aiogram.Bot для gwbotme.refresh: getMe отвечает ботом или
    падает, как при отозванном токене или закрытой сети. Возвращает список
    токенов, по которым спрашивали."""
    import aiogram
    asked = []

    def _bot(token, *a, **k):
        asked.append(token)
        me = types.SimpleNamespace(username=username, first_name=first_name)
        return types.SimpleNamespace(
            get_me=_async(me, RuntimeError("Unauthorized") if fail else None),
            session=types.SimpleNamespace(close=_async()))
    monkeypatch.setattr(aiogram, "Bot", _bot)
    return asked


def _forbid_setup(services, monkeypatch):
    """Всё, что меняет устройство, ключи или выпускает файл первого
    применения, — под запретом: любой вызов записывается."""
    touched = []

    def _rec(name):
        async def _a(*a, **k):
            touched.append(name)

        def _s(*a, **k):
            touched.append(name)
        return _a, _s
    for name in ("_gateway_new_go", "_gateway_mark_go"):
        monkeypatch.setattr(sh, name, _rec(name)[0])
    monkeypatch.setattr(services, "gateway_setup", _rec("gateway_setup")[1])
    return touched


async def _ask_token(services, fake_bot, slot=2):
    st = FakeState()
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_token(cb, GwSlotCB(action="token", slot=slot), services, st)
    return st, cb, nav


# ── кнопка на экране «⚙️ Конфигурация шлюза» ────────────────────────────────

def test_bundle_screen_of_a_slot_offers_the_token_button():
    """Кнопка есть только у экрана слота и ведёт в ввод токена именно этого
    слота; «Выпустить файл» — первой, «Отмена» — последней, как было."""
    markup = kb.settings_routing_bundle(2)
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["📤 Выпустить файл", TOKEN_BTN, "✖️ Отмена"], [b.text for b in buttons]
    assert buttons[1].callback_data == GwSlotCB(action="token", slot=2).pack(), buttons[1].callback_data
    assert buttons[2].callback_data == GwSlotCB(action="card", slot=2).pack(), "отмена ушла не в карточку слота"


def test_bundle_screen_without_a_slot_has_no_token_button():
    """Без слота (первая машина, раздел маршрутизации) токен спрашивают при
    заведении слота — отдельная кнопка там вела бы в никуда."""
    labels = _labels(kb.settings_routing_bundle(0))
    assert TOKEN_BTN not in labels, labels
    assert labels == ["📤 Выпустить файл", "✖️ Отмена"], labels


async def test_slot_bundle_screen_shows_the_token_button(services, slots, fake_bot):
    """Сквозь хендлер: экран «Конфигурация шлюза» слота 2 действительно
    показывает кнопку."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    cb, nav = _acb(fake_bot)
    await sh.gw_slot_bundle(cb, GwSlotCB(action="bundle", slot=2), services)
    shown = next(s for s in reversed(nav.sent) if s[0] == "edit_text")
    datas = {b.text: b.callback_data for row in shown[2].inline_keyboard for b in row}
    assert datas.get(TOKEN_BTN) == GwSlotCB(action="token", slot=2).pack(), datas


# ── приглашение к вводу ──────────────────────────────────────────────────────

def test_token_only_ask_names_the_known_bot_and_escapes():
    """Бот уже известен — человек видит, какой бот будет заменён; имя задаёт
    человек в BotFather: `<` без экранирования Telegram отвергает всё
    сообщение, и приглашение не пришло бы вовсе."""
    got = texts.gateway_token_only_ask("«A<B>»", {"username": "pi2_gw_bot", "name": "Шлюз <Pi2> & co"})
    assert got.startswith("🤖 <b>Токен бота шлюза «A&lt;B&gt;»</b>"), got
    assert "Сейчас известен бот Шлюз &lt;Pi2&gt; &amp; co (@pi2_gw_bot); новый токен заменит его." in got, got
    assert "пока не известен" not in got
    assert "<Pi2>" not in got and "<B>" not in got, got


def test_token_only_ask_falls_back_to_username_without_a_name():
    got = texts.gateway_token_only_ask("«Pi2»", {"username": "pi2_gw_bot", "name": ""})
    assert "Сейчас известен бот pi2_gw_bot (@pi2_gw_bot)" in got, got


def test_token_only_ask_without_a_known_bot():
    """Бота не спрашивали (или ответ был о старом токене) — честно «не
    известен», без пустых скобок и «@»."""
    for known in (None, {}, {"username": "", "name": "x"}):
        got = texts.gateway_token_only_ask("«Pi2»", known)
        assert "Бот этого шлюза серверу пока не известен" in got, (known, got)
        assert "Сейчас известен" not in got and "(@" not in got, (known, got)
        assert "<code>123456789:AA…</code>" in got, "нет образца, как выглядит токен"


async def test_token_button_asks_for_the_slot_token_with_cancel_to_the_card(services, slots, fake_bot):
    """Нажатие — приглашение на месте экрана, FSM ждёт токен именно этого
    слота в режиме «только токен»; «Отмена» — в карточку слота."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    st, cb, nav = await _ask_token(services, fake_bot)
    assert await st.get_state() == sh.GatewayToken.value.state, await st.get_state()
    data = await st.get_data()
    assert data.get("gw_slot") == 2 and data.get("gw_token_only") is True, data
    shown = next(s for s in reversed(nav.sent) if s[0] == "edit_text")
    assert shown[1] == texts.gateway_token_only_ask("«Pi2»", {}), shown[1]
    assert "пока не известен" in shown[1]
    btns = [b for row in shown[2].inline_keyboard for b in row]
    assert [(b.text, b.callback_data) for b in btns] == [("✖️ Отмена", GwSlotCB(action="card", slot=2).pack())], \
        [(b.text, b.callback_data) for b in btns]
    assert cb.answers, "спиннер кнопки не погашен"


async def test_token_button_shows_the_bot_already_known(services, slots, fake_bot):
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _known_bot(services, 2)
    _, _, nav = await _ask_token(services, fake_bot)
    shown = next(s for s in reversed(nav.sent) if s[0] == "edit_text")[1]
    assert "Сейчас известен бот Шлюз &lt;Pi2&gt; (@pi2_gw_bot)" in shown, shown


async def test_token_button_for_a_removed_slot_does_not_start_input(services, slots, fake_bot):
    """Кнопка из старого сообщения, слот уже снят: ввод не начинается, иначе
    следующий текст человека ушёл бы в токен несуществующего слота."""
    _, pi, _ = slots
    _slot1(services, pi)
    st, cb, nav = await _ask_token(services, fake_bot, slot=2)
    assert await st.get_state() is None and await st.get_data() == {}, "FSM ввода токена запущен для снятого слота"
    assert not [s for s in nav.sent if s[0] == "edit_text"], "приглашение показано для снятого слота"
    assert cb.answers and cb.answers[-1][1] is True, f"человеку не сказали, что слота нет: {cb.answers}"


# ── ввод токена ──────────────────────────────────────────────────────────────

async def test_token_input_saves_and_shows_the_card_with_the_bot(services, slots, fake_bot, monkeypatch):
    """Токен сохранён за слотом 2, Telegram спрошен по нему, карточка слота
    приходит сразу со ссылкой на бота. Устройство, ключи и файл первого
    применения не тронуты: шлюз уже работает, ввод токена его не переустанавливает."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    services.token[1] = "111111111:AA-first-token-value-long-enough"
    gws_before = services.db.gateways()
    dev_before = services.db.get_device(pi2.id)
    asked = _telegram(monkeypatch)
    touched = _forbid_setup(services, monkeypatch)
    st, _, _ = await _ask_token(services, fake_bot)
    msg = _amsg(fake_bot, TOKEN2)
    await sh.gateway_token_received(msg, st, services)

    assert services.token[2] == TOKEN2, "токен не сохранён за слотом 2"
    assert services.token[1] == "111111111:AA-first-token-value-long-enough", "ввод для слота 2 затёр токен слота 1"
    assert asked == [TOKEN2], f"getMe спрошен не по введённому токену: {asked}"
    assert touched == [], f"ввод токена полез в устройство/ключи: {touched}"
    assert services.runs == [], f"ввод токена запустил скрипт линка: {services.runs}"
    assert services.db.gateways() == gws_before, "ввод токена изменил слоты"
    dev_after = services.db.get_device(pi2.id)
    keys = lambda d: (d.private_key, d.public_key, d.preshared_key, d.address)   # noqa: E731
    assert keys(dev_after) == keys(dev_before), "ключи или адрес устройства шлюза сменились"
    assert not [s for s in msg.sent if s[0] == "document"], "ввод токена выдал файл"
    assert await st.get_state() is None, "FSM не закрыт после ввода"
    assert msg.deleted, "токен остался в истории чата"

    answers = [s for s in msg.sent if s[0] == "answer"]
    assert len(answers) == 1, [a[1] for a in answers]
    text, markup = answers[0][1], answers[0][2]
    assert text.startswith("🛰 <b>Шлюз «Pi2»</b>") and text.endswith("\n\n" + AGENT), text
    assert WARN not in text
    assert _back(markup) == LIST, "карточка после ввода — не с обычным выходом в список"


async def test_token_input_when_telegram_does_not_answer(services, slots, fake_bot, monkeypatch):
    """Telegram не ответил: токен всё равно сохранён, человек видит
    предупреждение — один раз и до карточки, — а карточка без строки «Бот
    шлюза», чтобы не вести в чат бота, о котором ничего не известно."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _telegram(monkeypatch, fail=True)
    touched = _forbid_setup(services, monkeypatch)
    st, _, _ = await _ask_token(services, fake_bot)
    msg = _amsg(fake_bot, TOKEN2)
    await sh.gateway_token_received(msg, st, services)

    assert services.token[2] == TOKEN2, "токен не сохранён после отказа getMe"
    assert touched == [] and services.runs == []
    answers = [s for s in msg.sent if s[0] == "answer"]
    assert len(answers) == 2, [a[1] for a in answers]
    assert answers[0][1].startswith(WARN), answers[0][1]
    assert sum(WARN in a[1] for a in answers) == 1, "предупреждение пришло не один раз"
    card = answers[1][1]
    assert card.startswith("🛰 <b>Шлюз «Pi2»</b>") and "Бот шлюза" not in card, card
    assert _back(answers[1][2]) == LIST


async def test_token_input_replacing_a_known_bot_with_a_dead_token_drops_the_old_link(
        services, slots, fake_bot, monkeypatch):
    """Бот был известен, ввели другой токен, getMe не ответил: ссылка на
    прежнего бота в карточке увела бы человека не в тот чат."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _known_bot(services, 2)
    _telegram(monkeypatch, fail=True)
    st, _, _ = await _ask_token(services, fake_bot)
    msg = _amsg(fake_bot, "333333333:CC-third-token-value-long-enough")
    await sh.gateway_token_received(msg, st, services)
    card = [s for s in msg.sent if s[0] == "answer"][-1][1]
    assert "pi2_gw_bot" not in card, card


async def test_invalid_token_is_refused_and_input_continues(services, slots, fake_bot, monkeypatch):
    """Не токен — отказ с причиной, FSM ждёт дальше, ничего не сохранено и
    Telegram не спрошен; карточка не приходит, пока не введён настоящий."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    # настоящая проверка формата: фикстура сцены подменяет сохранение словарём
    monkeypatch.setattr(services, "set_gw_bot_token", type(services).set_gw_bot_token.__get__(services))
    asked = _telegram(monkeypatch)
    touched = _forbid_setup(services, monkeypatch)
    st, _, _ = await _ask_token(services, fake_bot)
    msg = _amsg(fake_bot, "not-a-token")
    await sh.gateway_token_received(msg, st, services)
    answers = [s[1] for s in msg.sent if s[0] == "answer"]
    assert answers == ["⚠️ это не похоже на токен бота — жду строку вида 123456789:AA…"], answers
    assert 2 not in services.token, "некорректный токен сохранён"
    assert asked == [] and touched == [], (asked, touched)
    assert await st.get_state() == sh.GatewayToken.value.state, "после отказа ввод токена закрылся"
    assert (await st.get_data()).get("gw_token_only") is True, "режим «только токен» потерян после отказа"
    assert msg.deleted, "непринятый токен остался в истории"


async def test_token_input_keeps_the_home_exit_of_a_card_opened_from_home(services, slots, fake_bot, monkeypatch):
    """Карточку открыли ссылкой с главного экрана, оттуда — «Конфигурация» и
    токен: карточка после ввода по-прежнему уводит «Назад» на главную."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _telegram(monkeypatch)
    await _deeplink(services, fake_bot)
    st, _, _ = await _ask_token(services, fake_bot)
    msg = _amsg(fake_bot, TOKEN2)
    await sh.gateway_token_received(msg, st, services)
    markup = [s for s in msg.sent if s[0] == "answer"][-1][2]
    assert _back(markup) == HOME, "после ввода токена выход карточки с главной сменился"


async def test_token_input_for_a_single_slot_goes_back_to_the_section(services, slots, fake_bot, monkeypatch):
    """Единственный шлюз (слот 1): токен ложится за слотом 1, «Назад» с
    карточки — в раздел маршрутизации, как у обычной карточки."""
    _, pi, _ = slots
    _slot1(services, pi)
    _telegram(monkeypatch)
    touched = _forbid_setup(services, monkeypatch)
    st, _, _ = await _ask_token(services, fake_bot, slot=1)
    msg = _amsg(fake_bot, TOKEN2)
    await sh.gateway_token_received(msg, st, services)
    assert services.token.get(1) == TOKEN2 and 2 not in services.token, services.token
    assert touched == [] and [g.id for g in services.db.gateways()] == [1], "ввод токена завёл новый слот"
    markup = [s for s in msg.sent if s[0] == "answer"][-1][2]
    assert _back(markup) == SetCB(sec="rt").pack()
