"""Аварийная смена токена бота шлюза на стороне основного бота — что нажатия
меняют: «⚙️ Настройки → 🔧 Сервис → 🤖 Токен бота» (кнопка — только при
агенте с token1; один шлюз — сразу приглашение, несколько — выбор, ни
одного — всплывашка) → ввод токена (сообщение с токеном удаляется, getMe
новым токеном, бот не сервера и не другого шлюза, запись в env ВПС) →
«🔧 Сервис» с итогом первой строкой (новый бот — и строкой про «Старт») и документ с разделом token в канал при живой сессии; отказ —
переспрос, ввод не сброшен; «✖️ Отмена» — обратно в «🔧 Сервис».

Цена ошибки: токен в чате после ввода — секрет у всех, кто видит переписку;
бот сервера или бот другого шлюза, записанный в env, — два процесса на
одном боте перехватывают апдейты друг у друга; новый бот без напоминания
про «Старт» — бот шлюза не может написать человеку первым; ошибка ввода, сбросившая состояние, — следующий токен человек шлёт
в никуда (он уйдёт обычным сообщением в чат); документ, не ушедший при
живой сессии, — шлюз ждёт такта сверки, пока бот шлюза мёртв. Сами экраны —
в эталоне tests/screens/admin.txt (adm.set.svc.token*, adm.gtk.*).

Telegram подменён двойником `_Telegram` вместо aiogram.Bot (известные ему
токены — живые, прочие — 401); env ВПС — настоящий файл во временном
каталоге (AWG_BOT_ENV); слушатель канала — боевой LinkServer с одной сессией
из теста смены ключей линка.
"""
from __future__ import annotations

import types

import pytest

from awgbot.bot.callbacks import CancelCB, SetCB
from awgbot.bot.handlers import settings as sh
from awgbot.bot.handlers.reply_commands import on_cancel_inline
from awgbot.bot import texts
from awgbot.bot.texts import GATEWAY_TOKEN_QUEUED, GATEWAY_TOKEN_SENT
from awgbot.core import config
from awgbot.domain import gwcfg
from awgbot.runtime import gwbotme
from tests.conftest import FakeCallback, FakeMessage, FakeState
from tests.e2e import test_gateway_rekey_ui as _rekey
from tests.e2e import test_gateway_slots_ui as _slots_ui
from tests.e2e.test_gateway_slots_ui import _slot1, _slot2

pytestmark = pytest.mark.e2e
ADMIN = config.ADMIN_ID

slots = _slots_ui.slots
listener = _rekey.listener

OLD = "123456789:AA-dummy-token-for-tests"
NEW = "123456789:AA-new-dummy-token-tests"
OTHER = "987654321:BB-other-bot-token-dummy"         # новый бот (прежнего удалили)
SERVER = "111111111:AA-server-dummy-token"          # бот сервера AWG
TAKEN = "222222222:BB-taken-bot-token-dummy"         # бот второго шлюза
DEAD = "123456789:AA-dead-dummy-token-tests"
TOKEN1 = ("cfg1", "confirm1", "token1")


class _Telegram:
    """Двойник Telegram для aiogram.Bot: bots — токен → (id, username);
    прочие токены — 401, как отозванный. calls — по каким токенам спрашивали."""

    def __init__(self):
        self.bots = {OLD: (123456789, "naspi_gw_bot"), NEW: (123456789, "naspi_gw_bot"),
                     OTHER: (987654321, "other_gw_bot"), SERVER: (111111111, "awg_srv_bot"),
                     TAKEN: (222222222, "pi2_gw_bot")}
        self.calls: list[str] = []
        self.during = None          # корутина, которая выполняется, пока Telegram «думает»
        self.fail: BaseException | None = None   # чем getMe упадёт вместо ответа

    def factory(self, token, *a, **k):
        tg = self

        async def get_me():
            tg.calls.append(token)
            if tg.during is not None:
                await tg.during()
            if tg.fail is not None:
                raise tg.fail
            if token not in tg.bots:
                from aiogram.exceptions import TelegramUnauthorizedError
                from aiogram.methods import GetMe
                raise TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")
            bot_id, username = tg.bots[token]
            return types.SimpleNamespace(id=bot_id, username=username, first_name="NASPi", is_bot=True)

        async def close():
            pass
        return types.SimpleNamespace(get_me=get_me, session=types.SimpleNamespace(close=close))


@pytest.fixture()
def tg(monkeypatch):
    import aiogram
    t = _Telegram()
    monkeypatch.setattr(aiogram, "Bot", t.factory)
    monkeypatch.setattr(gwbotme, "_next_try", {})
    return t


@pytest.fixture()
def env(services, slots, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", SERVER)
    """env ВПС — настоящий файл: токены слотов читает и пишет боевой код
    (подмены фикстуры слотов сняты)."""
    path = tmp_path / "env"
    path.write_text(f"BOT_TOKEN=12345:main-bot\nGW_BOT_TOKEN={OLD}\n", encoding="utf-8")
    monkeypatch.setenv("AWG_BOT_ENV", str(path))
    monkeypatch.delattr(services, "gw_bot_token")
    monkeypatch.delattr(services, "set_gw_bot_token")
    return path


def _acb(bot):
    nav = FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=bot)
    return FakeCallback(message=nav, user_id=ADMIN, bot=bot), nav


def _rows(markup):
    return [[b.text for b in r] for r in markup.inline_keyboard]


def _edits(nav):
    return [(t, m) for k, t, m in nav.sent if k == "edit_text"]


def _caps(services, slot: int = 1, caps=TOKEN1) -> None:
    services.gwlink_session_opened(slot, "3.3.0", 2, list(caps))


async def _service(fake_bot, services):
    cb, nav = _acb(fake_bot)
    await sh.open_section(cb, SetCB(sec="svc"), services, FakeState())
    return _edits(nav)[-1]


async def _gtk(fake_bot, services, state, key: str, slot: int = 0):
    cb, nav = _acb(fake_bot)
    await sh.gwtoken_action(cb, SetCB(sec="gtk", act="do", key=key, val=str(slot) if slot else ""),
                            services, state)
    return cb, nav


async def _send(fake_bot, services, state, text: str) -> FakeMessage:
    msg = FakeMessage(text=text, chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await sh.gwtoken_received(msg, state, services)
    return msg


async def _asked(fake_bot, services, slot: int = 1) -> FakeState:
    """Приглашение к вводу токена слота открыто."""
    state = FakeState()
    await _gtk(fake_bot, services, state, "ask", slot)
    assert await state.get_state() == "GatewayBotToken:value"
    return state


# ── кнопка в «🔧 Сервис» ─────────────────────────────────────────────────────

async def test_service_shows_the_token_button_only_for_an_agent_with_token1(services, slots, env, fake_bot):
    """Агент без token1 — кнопки нет (довезти токен нечем); с confirm1, но
    без token1 — только «🔑 Ключи линка»; с token1, но без confirm1 — только
    «🤖 Токен бота»; с обеими — рядом одним рядом перед «Назад»."""
    _, pi, _ = slots
    _slot1(services, pi)
    _, markup = await _service(fake_bot, services)
    assert "🤖 Токен бота" not in sum(_rows(markup), []), _rows(markup)
    _caps(services, 1, ("cfg1", "confirm1"))
    _, markup = await _service(fake_bot, services)
    assert _rows(markup)[-2:] == [["🔑 Ключи линка"], ["⬅️ Назад"]], _rows(markup)
    _caps(services, 1, ("cfg1", "token1"))
    _, markup = await _service(fake_bot, services)
    assert _rows(markup)[-2:] == [["🤖 Токен бота"], ["⬅️ Назад"]], _rows(markup)
    _caps(services)
    _, markup = await _service(fake_bot, services)
    assert _rows(markup)[-2:] == [["🔑 Ключи линка", "🤖 Токен бота"], ["⬅️ Назад"]], _rows(markup)
    data = [b.callback_data for r in markup.inline_keyboard for b in r if b.text == "🤖 Токен бота"]
    assert data == [SetCB(sec="gtk", act="do", key="pick").pack()], data


# ── выбор шлюза ──────────────────────────────────────────────────────────────

async def test_pick_without_agents_is_an_alert(services, slots, env, fake_bot):
    """Устаревшая кнопка без шлюзов с token1 — всплывашка-объяснение, экран
    не тронут, ввод не открыт."""
    _, pi, _ = slots
    _slot1(services, pi)
    _caps(services, 1, ("cfg1", "confirm1"))
    state = FakeState()
    cb, nav = await _gtk(fake_bot, services, state, "pick")
    assert cb.answers[-1] == ("Довезти токен каналом некому: нужен агент шлюза 3.3.0", True), cb.answers
    assert not _edits(nav) and await state.get_state() is None


async def test_pick_with_one_agent_opens_the_prompt_at_once(services, slots, env, fake_bot):
    """Один шлюз с token1 — сразу приглашение с именем шлюза и «✖️ Отмена»
    в «Сервис»: экран выбора из одного пункта — лишнее нажатие."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _caps(services, 1)
    state = FakeState()
    _, nav = await _gtk(fake_bot, services, state, "pick")
    text, markup = _edits(nav)[-1]
    assert "Новый токен бота NASPi" in text and "сразу удалю" in text, text
    assert "не бота сервера и не бота другого шлюза" in text, text
    assert _rows(markup) == [["✖️ Отмена"]], _rows(markup)
    assert markup.inline_keyboard[0][0].callback_data == CancelCB(kind="set_svc", ref=0).pack()
    assert await state.get_state() == "GatewayBotToken:value"
    assert (await state.get_data())["gw_slot"] == 1


async def test_pick_with_two_agents_shows_a_choice(services, slots, env, fake_bot):
    """Два шлюза с token1 — выбор по имени с подписью; кнопка слота открывает
    приглашение именно этого слота; «⬅️ Назад» — в «Сервис»."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _caps(services, 1); _caps(services, 2)
    services.gateway_set_label(2, "север")
    state = FakeState()
    _, nav = await _gtk(fake_bot, services, state, "pick")
    text, markup = _edits(nav)[-1]
    assert text.startswith("🤖 Токен бота шлюза"), text
    rows = [[(b.text, b.callback_data) for b in r] for r in markup.inline_keyboard]
    assert rows == [[("NASPi", SetCB(sec="gtk", act="do", key="ask", val="1").pack())],
                    [("Pi2 (север)", SetCB(sec="gtk", act="do", key="ask", val="2").pack())],
                    [("⬅️ Назад", SetCB(sec="svc").pack())]], rows
    assert await state.get_state() is None, "выбор уже открыл ввод"
    _, nav = await _gtk(fake_bot, services, state, "ask", 2)
    assert "Новый токен бота Pi2 (север)" in _edits(nav)[-1][0]
    assert (await state.get_data())["gw_slot"] == 2


async def test_ask_for_a_slot_without_token1_is_stale(services, slots, env, fake_bot):
    """Кнопка слота со старого экрана, а агент откатили на версию без token1 —
    «устарело», ввод не открыт."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _caps(services, 1)
    state = FakeState()
    cb, nav = await _gtk(fake_bot, services, state, "ask", 2)
    assert cb.answers and cb.answers[-1][1] is True, cb.answers
    assert not _edits(nav) and await state.get_state() is None


# ── ввод токена ──────────────────────────────────────────────────────────────

async def test_the_same_bot_token_is_stored_and_service_says_it_went(services, slots, env, tg, listener, fake_bot):
    """Тот же бот, шлюз на связи: сообщение с токеном удалено, в env ВПС —
    новый токен слота (BOT_TOKEN основного бота цел), «🔧 Сервис» новым
    сообщением с итогом «ушёл шлюзу» первой строкой, ввод закрыт; в канал
    ушёл ровно один документ с новым токеном."""
    _, pi, _ = slots
    _slot1(services, pi)
    _caps(services)
    services.gateway_token_replace(1, OLD)            # прежний токен когда-то вводили кнопкой
    have = services.gwcfg_doc(services.db.gateway(1))["prints"]
    assert have["token"] == gwcfg.token_print(OLD)
    services.gwlink_snapshot_in(1, {"agent_version": "3.3.0", "rev": 1, "cfg": {"gen": 1, **have}}, 1, True)
    listener.open(1, caps=TOKEN1)
    state = await _asked(fake_bot, services)
    msg = await _send(fake_bot, services, state, NEW)
    assert msg.deleted, "сообщение с токеном осталось в чате"
    lines = env.read_text(encoding="utf-8").splitlines()
    assert f"GW_BOT_TOKEN={NEW}" in lines and "BOT_TOKEN=12345:main-bot" in lines, lines
    assert OLD not in env.read_text(encoding="utf-8")
    assert services.gateway_token_pushed(1, NEW), "нет метки «введён кнопкой» — раздел token не поедет"
    assert await state.get_state() is None, "ввод не закрыт после успеха"
    answers = [(t, m) for k, t, m in msg.sent if k == "answer"]
    text, markup = answers[-1]
    assert text.startswith(GATEWAY_TOKEN_SENT + "\n\n🔧 <b>Сервис</b>"), text.splitlines()[:3]
    assert "🔁 Перезапуск AWG" in sum(_rows(markup), [])
    assert tg.calls[0] == NEW, "getMe спросили не новым токеном"
    task = listener.__dict__.get("_deliver_task")
    assert task is not None, "доставку не позвали"
    await task
    cfgs = [m for m in listener.sent(1) if m["t"] == "cfg"]
    assert len(cfgs) == 1 and cfgs[0]["token"] == {"value": NEW}, [m["t"] for m in listener.sent(1)]


async def test_offline_gateway_gets_the_queued_note(services, slots, env, tg, fake_bot):
    """Шлюз не на связи — токен записан, итог «получит при подключении»."""
    _, pi, _ = slots
    _slot1(services, pi)
    _caps(services)
    services.gwlink_session_closed(1)
    state = await _asked(fake_bot, services)
    msg = await _send(fake_bot, services, state, NEW)
    text = [t for k, t, _ in msg.sent if k == "answer"][-1]
    assert text.startswith(GATEWAY_TOKEN_QUEUED), text.splitlines()[0]
    assert f"GW_BOT_TOKEN={NEW}" in env.read_text(encoding="utf-8")


@pytest.mark.parametrize("token, why", [
    (SERVER, "это токен бота сервера AWG — шлюзу нужен свой бот"),
    (TAKEN, "этот бот уже у шлюза Pi2 — нужен свой бот для каждого шлюза"),
    (DEAD, "Telegram не принял этот токен"),
    ("привет", "не похоже на токен бота"),
])
async def test_a_wrong_token_is_asked_again_and_the_input_stays(services, slots, env, tg, fake_bot, token, why):
    """Бот сервера, бот другого шлюза, отозванный токен, не токен вовсе —
    сообщение всё равно удалено, env не тронут, переспрос с причиной, ввод
    открыт: следующий токен человек шлёт в тот же диалог."""
    _, pi, pi2 = slots
    _slot1(services, pi); _slot2(services, pi2)
    _caps(services)
    services.set_gw_bot_token(TAKEN, 2)
    before = env.read_text(encoding="utf-8")
    state = await _asked(fake_bot, services)
    msg = await _send(fake_bot, services, state, token)
    assert msg.deleted, "сообщение с токеном осталось в чате"
    assert env.read_text(encoding="utf-8") == before, "env тронут отвергнутым токеном"
    answers = [t for k, t, _ in msg.sent if k == "answer"]
    assert len(answers) == 1 and answers[0].startswith("⚠️ Токен не принят:") and why in answers[0], answers
    assert answers[0].endswith("Пришли другой токен или нажми «✖️ Отмена»"), answers[0]
    assert await state.get_state() == "GatewayBotToken:value", "ошибка сбросила ввод"
    msg = await _send(fake_bot, services, state, NEW)
    assert f"GW_BOT_TOKEN={NEW}" in env.read_text(encoding="utf-8"), "после переспроса верный токен не принят"


async def test_a_new_bot_is_taken_and_the_result_asks_to_press_start(services, slots, env, tg, fake_bot):
    """Токен нового бота (прежнего удалили вовсе) — принят: в env ВПС он,
    итог второй строкой просит открыть @нового бота и нажать «Старт» — без
    этого бот шлюза не сможет написать человеку первым."""
    _, pi, _ = slots
    _slot1(services, pi)
    _caps(services)
    services.gwlink_session_closed(1)
    state = await _asked(fake_bot, services)
    msg = await _send(fake_bot, services, state, OTHER)
    assert f"GW_BOT_TOKEN={OTHER}" in env.read_text(encoding="utf-8")
    text = [t for k, t, _ in msg.sent if k == "answer"][-1]
    head = text.split("\n\n", 1)[0].splitlines()
    assert head == [GATEWAY_TOKEN_QUEUED, texts.gateway_token_new_bot("other_gw_bot")], head
    assert "@other_gw_bot" in head[1] and "«Старт»" in head[1], head[1]


async def test_the_same_bot_has_no_start_line(services, slots, env, tg, fake_bot):
    """Тот же бот — итог одной строкой: «Старт» у него уже нажат."""
    _, pi, _ = slots
    _slot1(services, pi)
    _caps(services)
    state = await _asked(fake_bot, services)
    msg = await _send(fake_bot, services, state, NEW)
    text = [t for k, t, _ in msg.sent if k == "answer"][-1]
    assert "«Старт»" not in text and text.split("\n\n", 1)[0] == GATEWAY_TOKEN_QUEUED, text.splitlines()[:2]


def test_the_new_bot_line_without_a_name():
    """Telegram не дал username — «нового бота», а не «@» в пустоту."""
    assert texts.gateway_token_new_bot("").startswith("Бот шлюза теперь нового бота:")
    assert texts.gateway_token_new_bot("a<b").startswith("Бот шлюза теперь @a&lt;b:")


async def test_without_the_old_token_the_snapshot_name_decides(services, slots, env, tg, fake_bot):
    """Токена слота на ВПС нет (слот старше вопроса о токене) — тот же бот
    узнаётся по имени из снимка канала: строки про «Старт» нет."""
    _, pi, _ = slots
    _slot1(services, pi)
    _caps(services)
    env.write_text("BOT_TOKEN=12345:main-bot\n", encoding="utf-8")
    services.gwlink_snapshot_in(1, {"agent_bot": {"username": "naspi_gw_bot", "name": "NASPi"}, "rev": 2}, 2, True)
    state = await _asked(fake_bot, services)
    msg = await _send(fake_bot, services, state, NEW)
    assert f"GW_BOT_TOKEN={NEW}" in env.read_text(encoding="utf-8")
    assert "«Старт»" not in [t for k, t, _ in msg.sent if k == "answer"][-1]


async def test_cancel_returns_to_service(services, slots, env, fake_bot):
    """«✖️ Отмена» под приглашением — «🔧 Сервис» на месте приглашения,
    ввод сброшен, env не тронут."""
    _, pi, _ = slots
    _slot1(services, pi)
    _caps(services)
    before = env.read_text(encoding="utf-8")
    state = await _asked(fake_bot, services)
    cb, nav = _acb(fake_bot)
    await on_cancel_inline(cb, CancelCB(kind="set_svc", ref=0), state, services, role="admin")
    text, markup = _edits(nav)[-1]
    assert text.startswith("🔧 <b>Сервис</b>"), text.splitlines()[0]
    assert "🤖 Токен бота" in sum(_rows(markup), [])
    assert await state.get_state() is None and env.read_text(encoding="utf-8") == before


async def test_cancel_while_telegram_thinks_writes_nothing(services, slots, env, tg, listener, fake_bot):
    """«✖️ Отмена», нажатая, пока getMe ещё не ответил: ввод уже снят — токен
    не пишется в env, метки нет, документ шлюзу не уходит; человек отменил —
    значит отменил."""
    _, pi, _ = slots
    _slot1(services, pi)
    _caps(services)
    listener.open(1, caps=TOKEN1)
    before = env.read_text(encoding="utf-8")
    state = await _asked(fake_bot, services)

    async def cancel():
        cb, _ = _acb(fake_bot)
        await on_cancel_inline(cb, CancelCB(kind="set_svc", ref=0), state, services, role="admin")
    tg.during = cancel
    msg = await _send(fake_bot, services, state, NEW)
    assert msg.deleted, "сообщение с токеном осталось в чате"
    assert env.read_text(encoding="utf-8") == before, "отменённый ввод записал токен"
    assert services.gateway_token_pushed(1, NEW) is False
    assert listener.__dict__.get("_deliver_task") is None and listener.sent(1) == [], "документ ушёл после отмены"
    assert not [k for k, *_ in msg.sent if k == "answer"], "после отмены пришёл итог или переспрос"


async def test_a_network_error_does_not_echo_the_token(services, slots, env, tg, fake_bot):
    """Сеть до Telegram оборвалась — текст исключения aiohttp несёт URL с
    токеном; в переспросе его быть не должно (чат — не место для секрета):
    фиксированное «Telegram не ответил — пришли токен ещё раз», ввод открыт."""
    _, pi, _ = slots
    _slot1(services, pi)
    _caps(services)
    tg.fail = RuntimeError(f"Cannot connect to https://api.telegram.org/bot{NEW}/getMe")
    state = await _asked(fake_bot, services)
    msg = await _send(fake_bot, services, state, NEW)
    answers = [t for k, t, _ in msg.sent if k == "answer"]
    assert len(answers) == 1, answers
    assert NEW not in answers[0] and NEW.split(":", 1)[1] not in answers[0], "токен в переспросе"
    assert "Telegram не ответил — пришли токен ещё раз" in answers[0], answers[0]
    assert await state.get_state() == "GatewayBotToken:value"
