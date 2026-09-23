"""Ссылки из экранов в сторону шлюза: строка «Бот шлюза» последней в
карточке слота и в карточке устройства-шлюза, deep-link «/start gw-<слот>»
из строки РФ-доступа в шапке админа и getMe сразу после ввода токена."""
from __future__ import annotations

import types

import pytest

from awgbot.bot import texts
from awgbot.bot.callbacks import DeviceCB, GwMarkCB, GwSlotCB, SetCB
from awgbot.bot.handlers import admin as ah
from awgbot.bot.handlers import settings as sh
from awgbot.core import config
from awgbot.runtime import gwbotme
from tests.conftest import FakeState
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
        assert shown and shown[-1] == "🛰 Такого шлюза больше нет — слот снят.", (payload, shown)
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


def _async(value):
    async def _f(*a, **k):
        return value
    return _f
