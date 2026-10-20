"""Раздел «🛟 Резерв» основного бота — что нажатия меняют: раздел без пары
(Т-Р1), «📤 Пригласить» (живое сообщение Т-Р2), «✏️ Ввести код» с попытками
(Т-Р7), «⬅️ Отменить», «📥 Принять приглашение» (Т-Р3 → Т-Р4/Т-Р5 →
«🛟 Подключиться»), карточка (Т-Р9), тумблер готовности при паре (диалог),
«🗑 Удалить резерв» (отказ в переезде, диалог), «🆘 На крайний случай»
(Т-Р18: код отдельным сообщением, удаление через 10 минут, смена кода).

Хендлеры зовутся напрямую (FakeCallback/FakeMessage/FakeState); скрипт
линка пары и файервол — модели из tests/integration/_reserve_common; канал
пары — боевой PairServer без сокета (его pair_close снимает приглашение).
Экраны целиком — в эталоне tests/screens/admin.txt (adm.rsv.*).

Цена ошибки: строка приглашения с закрытым ключом, оставшаяся в чате
принявшего, — ключ линка у всех, кто видит переписку; третий неверный код
без закрытия — перебор кода; удаление пары, пока клиенты у резерва, —
клиенты без сервера; аварийный код, не удалённый из чата, — секрет в
истории навсегда.
"""
from __future__ import annotations

import asyncio

import pytest

from awgbot.bot.callbacks import RsvCB
from awgbot.bot.handlers.settings.reserve import accept as acc_h
from awgbot.bot.handlers.settings.reserve import card as card_h
from awgbot.bot.handlers.settings.reserve import events as ev
from awgbot.bot.handlers.settings.reserve import invite as inv_h
from awgbot.bot.texts.reserve import pairing as pt
from awgbot.core import config, settings
from awgbot.runtime import pairlink
from tests.conftest import FakeCallback, FakeMessage, FakeState, last_screen
from tests.integration import _reserve_common as rc
from tests.integration._reserve_common import Acceptor, Inviter

pytestmark = pytest.mark.e2e
ADMIN = config.ADMIN_ID
rsv = rc.rsv


@pytest.fixture()
def ui(services, rsv, fake_bot, make_active_client, monkeypatch):
    """Админ в чате, канал пары — PairServer без слушателя (только его
    действия из чата: pair_wrong/pair_close/pair_accepted)."""
    from awgbot.util import dnsapi
    monkeypatch.setattr(dnsapi, "read_system", lambda name, timeout=3.0: [])      # карточка не ходит в сеть за адресом
    make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    from awgbot.runtime.pairlink.server import PairServer
    srv = PairServer(services)

    async def no_listen():
        return None
    monkeypatch.setattr(srv, "ensure", no_listen)
    monkeypatch.setattr(pairlink, "_link", srv)
    return rsv


def _nav(fake_bot) -> FakeMessage:
    return FakeMessage(chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)


def _cb(nav, fake_bot) -> FakeCallback:
    return FakeCallback(message=nav, user_id=ADMIN, bot=fake_bot)


def _labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row] if markup else []


# ── раздел без пары и приглашение ───────────────────────────────────────────

async def test_the_section_without_a_pair_offers_invite_and_accept(services, ui):
    text, markup = await card_h.reserve_screen(services)
    assert "🛟 <b>Резерв</b>" in text and f"<code>{rc.HOST_A}</code>" in text
    assert _labels(markup) == ["✅ Готов быть резервом: вкл", "📤 Пригласить", "📥 Принять приглашение", "🌐 Запись DNS", "⬅️ Назад"]


async def test_an_ip_address_for_clients_is_a_stop_line(services, ui):
    settings.set_value("app.network.server_host", "203.0.113.5")
    text, _ = await card_h.reserve_screen(services)
    assert "⛔ Адрес для клиентов — IP <code>203.0.113.5</code>" in text


async def test_invite_sends_the_live_message_and_the_section_shows_it(services, ui, fake_bot):
    nav = _nav(fake_bot)
    cb = _cb(nav, fake_bot)
    await inv_h.invite(cb, services, FakeState())
    sent = [s for s in nav.sent if s[0] == "answer"]
    assert sent and "Приглашение в резерв" in sent[-1][1]
    token = services.reserve_invite()["token"]
    assert token in sent[-1][1] and _labels(sent[-1][2]) == ["✏️ Ввести код", "⬅️ Отменить"]
    assert services.reserve_invite()["msg"], "сообщение приглашения не запомнено — ход сопряжения его не поправит"
    text, labels = last_screen(nav)
    assert "Приглашение выпущено" in text and "📤 Пригласить" not in labels and "⬅️ Отменить приглашение" in labels


async def test_invite_refused_is_an_alert_and_nothing_is_sent(services, ui, fake_bot):
    ui.fw_fail = True
    nav = _nav(fake_bot)
    cb = _cb(nav, fake_bot)
    await inv_h.invite(cb, services, FakeState())
    assert cb.answers and cb.answers[-1][1] is True and "не выдано" in cb.answers[-1][0]
    assert not [s for s in nav.sent if s[0] == "answer"], "приглашение ушло в чат при отказе"


# ── ввод кода ────────────────────────────────────────────────────────────────

async def _invited_and_connected(services, ui, fake_bot):
    nav = _nav(fake_bot)
    await inv_h.invite(_cb(nav, fake_bot), services, FakeState())
    acc = Acceptor(services.reserve_invite()["id"])
    out = services.reserve_pair_hello(acc.hello())
    acc.eph_in(out["body"])
    return nav, acc


async def test_enter_code_before_the_reserve_connects_is_an_alert(services, ui, fake_bot):
    nav = _nav(fake_bot)
    await inv_h.invite(_cb(nav, fake_bot), services, FakeState())
    cb = _cb(nav, fake_bot)
    state = FakeState()
    await inv_h.code_ask(cb, RsvCB(action="code", pid=services.reserve_invite()["id"]), services, state)
    assert cb.answers[-1] == (pt.CODE_WAIT, True) and await state.get_state() is None


async def test_three_wrong_codes_close_the_invitation_and_drop_the_link(services, ui, fake_bot):
    nav, acc = await _invited_and_connected(services, ui, fake_bot)
    state = FakeState()
    cb = _cb(nav, fake_bot)
    await inv_h.code_ask(cb, RsvCB(action="code", pid=services.reserve_invite()["id"]), services, state)
    assert await state.get_state() == "ReserveInviteCode:value"
    wrong = "AAAAAA" if acc.code != "AAAAAA" else "BBBBBB"
    answers = []
    for _ in range(3):
        msg = FakeMessage(text=wrong, chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
        await inv_h.code_received(msg, services, state)
        answers += [s[1] for s in msg.sent]
    assert any("Осталось попыток: 2" in a for a in answers) and any("Осталось попыток: 1" in a for a in answers)
    assert answers[-1] == pt.CODE_REFUSED, answers[-1]
    assert services.reserve_invite() == {} and ui.modes[-1] == "--rollback", "линк пережил третий неверный код"
    assert any(r[0] == "edit_message_text" and r[2] == pt.CODE_REFUSED for r in fake_bot.records), \
        "живое сообщение приглашения не получило итог «трижды»"
    assert await state.get_state() is None


async def test_a_malformed_code_asks_again_and_keeps_the_input(services, ui, fake_bot):
    nav, _acc = await _invited_and_connected(services, ui, fake_bot)
    state = FakeState()
    await inv_h.code_ask(_cb(nav, fake_bot), RsvCB(action="code"), services, state)
    msg = FakeMessage(text="K7F", chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await inv_h.code_received(msg, services, state)
    assert msg.sent[-1][1] == pt.CODE_FORMAT and await state.get_state() == "ReserveInviteCode:value"
    assert services.reserve_invite()["attempts"] == 0


async def test_the_right_code_is_accepted(services, ui, fake_bot):
    nav, acc = await _invited_and_connected(services, ui, fake_bot)
    state = FakeState()
    await inv_h.code_ask(_cb(nav, fake_bot), RsvCB(action="code"), services, state)
    msg = FakeMessage(text=acc.code, chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await inv_h.code_received(msg, services, state)
    assert "Код верный" in msg.sent[-1][1] and services.reserve_invite()["state"] == "accepted"


async def test_cancel_drops_the_invitation_and_a_second_cancel_is_an_alert(services, ui, fake_bot):
    nav = _nav(fake_bot)
    await inv_h.invite(_cb(nav, fake_bot), services, FakeState())
    cb = _cb(nav, fake_bot)
    await inv_h.cancel(cb, services, FakeState())
    assert services.reserve_invite() == {} and ui.modes[-1] == "--rollback"
    assert any(r[0] == "edit_message_text" and r[2] == pt.INVITE_CANCELLED for r in fake_bot.records)
    cb2 = _cb(nav, fake_bot)
    await inv_h.cancel(cb2, services, FakeState())
    assert cb2.answers[-1] == (pt.CODE_GONE, True)


async def test_the_live_message_shows_two_addresses(services, ui, fake_bot):
    nav, acc = await _invited_and_connected(services, ui, fake_bot)
    out = services.reserve_pair_hello(Acceptor(services.reserve_invite()["id"]).hello(addr=rc.ADDR_X))
    await ev.on_event(fake_bot, services, out["event"], out["st"], {})
    edits = [r[2] for r in fake_bot.records if r[0] == "edit_message_text"]
    assert edits and rc.ADDR_B in edits[-1] and rc.ADDR_X in edits[-1] and "двух адресов" in edits[-1]


# ── принятие ─────────────────────────────────────────────────────────────────

async def test_a_pasted_invitation_is_deleted_and_the_dialog_puts_cancel_first(services, ui, fake_bot):
    nav = _nav(fake_bot)
    state = FakeState()
    await acc_h.accept_ask(_cb(nav, fake_bot), services, state)
    assert await state.get_state() == "ReserveAcceptToken:value"
    inv = Inviter()
    msg = FakeMessage(text=inv.token, chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await acc_h.token_received(msg, services, state)
    assert msg.deleted, "строка приглашения с закрытым ключом осталась в чате"
    text, markup = msg.sent[-1][1], msg.sent[-1][2]
    assert "Стать резервом" in text and f"<code>{rc.HOST_B}</code>" in text
    assert _labels(markup)[0] == "⬅️ Отмена", "«Отмена» не первая в диалоге с последствиями"
    assert _labels(markup)[-1] == "🛟 Подключиться"


async def test_a_refused_invitation_is_deleted_too_and_asked_again(services, ui, fake_bot):
    state = FakeState()
    await acc_h.accept_ask(_cb(_nav(fake_bot), fake_bot), services, state)
    msg = FakeMessage(text=Inviter(expires_in=-60).token, chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await acc_h.token_received(msg, services, state)
    assert msg.deleted, "отвергнутая строка с закрытым ключом осталась в чате"
    assert "Срок приглашения вышел" in msg.sent[-1][1]
    assert await state.get_state() == "ReserveAcceptToken:value", "после отказа ввод сброшен"


async def test_without_own_gateways_the_dialog_has_no_rf_toggle_and_warns(services, ui, fake_bot):
    ui.gw = False
    state = FakeState()
    await acc_h.accept_ask(_cb(_nav(fake_bot), fake_bot), services, state)
    msg = FakeMessage(text=Inviter().token, chat_id=ADMIN, user_id=ADMIN, bot=fake_bot)
    await acc_h.token_received(msg, services, state)
    text, markup = msg.sent[-1][1], msg.sent[-1][2]
    assert "шлюзов нет" in text and not [x for x in _labels(markup) if "РФ-доступ" in x]


async def test_connect_starts_the_link_and_shows_connecting(services, ui, fake_bot):
    state = FakeState()
    await state.update_data(token=Inviter().token, rf=True)
    nav = _nav(fake_bot)
    cb = _cb(nav, fake_bot)
    await acc_h.connect(cb, services, state)
    text, labels = last_screen(nav)
    assert text.startswith("🟡 Подключаюсь к") and labels == ["⬅️ Отменить"]
    assert services.reserve_invite()["role"] == "acceptor" and services.reserve_invite()["msg"]


async def test_the_acceptor_live_message_gets_the_code(services, ui, fake_bot):
    st = {"role": "acceptor", "peer_name": "Beta", "msg": [ADMIN, 901]}
    await ev.on_event(fake_bot, services, "code", st, {"code": "K7F2QX"})
    edits = [r[2] for r in fake_bot.records if r[0] == "edit_message_text"]
    assert edits and "K7F-2QX" in edits[-1] and "«Beta»" in edits[-1]


# ── карточка, тумблеры, удаление ────────────────────────────────────────────

@pytest.fixture()
def paired(services, ui):
    rc.pair_as_inviter(services, ui)
    return ui


async def test_the_card_names_the_peer_and_its_actions(services, paired):
    text, markup = await card_h.reserve_screen(services)
    assert "Резерв «Beta»" in text and rc.ADDR_B in text
    labels = _labels(markup)
    assert "🆘 На крайний случай" in labels and "🗑 Удалить резерв" in labels and labels[-1] == "⬅️ Назад"


async def test_readiness_off_with_a_pair_goes_through_a_dialog(services, paired, fake_bot):
    nav = _nav(fake_bot)
    cb = _cb(nav, fake_bot)
    await card_h.ready_toggle(cb, services, FakeState())
    text, labels = last_screen(nav)
    assert "Выключить «Готов быть резервом»?" in text and labels[0].startswith("⬅️"), labels
    assert settings.get_bool("app.reserve.ready", False) is True, "выключено без подтверждения"
    await card_h.ready_off_yes(_cb(nav, fake_bot), services)
    assert settings.get_bool("app.reserve.ready", True) is False


async def test_readiness_off_while_moving_is_deferred_in_the_toast(services, paired, fake_bot):
    services.reserve_set_in("taking")
    cb = _cb(_nav(fake_bot), fake_bot)
    await card_h.ready_off_yes(cb, services)
    assert "вступит после возврата" in cb.answers[-1][0], cb.answers


async def test_unpair_while_my_clients_are_away_is_refused(services, paired, fake_bot):
    services.reserve_set_out("away")
    nav = _nav(fake_bot)
    cb = _cb(nav, fake_bot)
    await card_h.unpair_ask(cb, services)
    assert cb.answers[-1][1] is True and "сначала верни" in cb.answers[-1][0]
    assert not [s for s in nav.sent if s[0] == "edit_text"], "диалог удаления открыт в переезде"
    assert services.reserve_paired()


async def test_unpair_through_the_dialog(services, paired, fake_bot):
    nav = _nav(fake_bot)
    await card_h.unpair_ask(_cb(nav, fake_bot), services)
    text, labels = last_screen(nav)
    assert "Удалить резерв «Beta»?" in text and labels[0].startswith("⬅️")
    await card_h.unpair_yes(_cb(nav, fake_bot), services)
    assert not services.reserve_paired() and paired.modes[-1] == "--rollback"
    assert any("Резерв «Beta» удалён" in s[1] for s in nav.sent if s[0] == "answer")
    text, labels = last_screen(nav)
    assert "📤 Пригласить" in labels, "после удаления не раздел без пары"


# ── на крайний случай ───────────────────────────────────────────────────────

async def test_the_emergency_screen_without_a_pair_has_no_code_buttons(services, ui, fake_bot):
    nav = _nav(fake_bot)
    await card_h.emergency(_cb(nav, fake_bot), services)
    text, labels = last_screen(nav)
    assert "Кода нет" in text and labels == ["⬅️ Назад"]


async def test_show_code_sends_it_once_and_deletes_it_after_ten_minutes(services, paired, fake_bot, monkeypatch):
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fast(delay, *a, **kw):
        slept.append(delay)
        await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", fast)
    cb = _cb(_nav(fake_bot), fake_bot)
    await card_h.show_code(cb, services)
    for _ in range(5):
        await real_sleep(0)
    code = services.reserve_code()
    sent = [r for r in fake_bot.records if r[0] == "send_message" and code in r[2]]
    assert len(sent) == 1 and sent[0][1] == ADMIN
    assert ev.CODE_TTL in slept and ev.CODE_TTL == 600, slept
    assert [r for r in fake_bot.records if r[0] == "delete_message"], "сообщение с кодом не удалено"
    assert services.reserve_keys()["code_shown_at"], "время показа не записано"


async def test_new_code_asks_first_and_changes_nothing_yet(services, paired, fake_bot):
    before = services.reserve_code()
    nav = _nav(fake_bot)
    await card_h.new_code_ask(_cb(nav, fake_bot), services)
    text, labels = last_screen(nav)
    assert labels and labels[0].startswith("⬅️"), "«Отмена» не первая в диалоге смены кода"
    assert services.reserve_code() == before and services.reserve_keys()["m_gen"] == 1, "код сменён без подтверждения"
    assert not [r for r in fake_bot.records if r[0] == "send_message"]


async def test_new_code_changes_the_generation_and_sends_the_new_one(services, paired, fake_bot, monkeypatch):
    monkeypatch.setattr(ev, "_delete_later", lambda *a, **k: asyncio.sleep(0))
    before = services.reserve_code()
    nav = _nav(fake_bot)
    await card_h.new_code(_cb(nav, fake_bot), services)
    after = services.reserve_code()
    assert after and after != before and services.reserve_keys()["m_gen"] == 2
    texts = [r[2] for r in fake_bot.records if r[0] == "send_message"]
    assert any(after in t and "сменился" in t for t in texts) and not any(before in t for t in texts)
    _, labels = last_screen(nav)
    assert labels[:2] == ["🔑 Показать код", "🔄 Сменить код"]
