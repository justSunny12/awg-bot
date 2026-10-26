"""Переезд кнопкой, возврат и запись DNS в боте — что нажатия показывают и
меняют: карточка с кнопками по возможностям, диалог перевода (Т-Р10, ручной и
автоматический режим), живое сообщение хода (Т-Р10б), «вернуть клиентов к
нему» у резерва (Т-Р12), экран «🌐 Запись DNS» (Т-Р16) и ввод токена,
строка главной (Т-Р17), кнопка «↩️ Вернуть запись» на уведомлении Т-РУ16.

Нажатия идут через настоящий диспетчер роли (AccessMiddleware, порядок
роутеров, разбор упакованных колбэков) на заглушке сессии Bot API: то, что
бот сказал Telegram, и проверяется.

Цена ошибки: кнопка «Перевести», показанная при невозможном переезде, —
админ жмёт и получает отказ вместо объяснения в карточке; диалог перевода,
не называющий, кто меняет запись, — человек не знает, что ему делать
руками; токен API DNS, оставшийся в чате, — доступ к зоне домена в истории
переписки; кнопка, перехваченная хендлером другой роли, — нажатие без
реакции в самый момент, когда она нужна.
"""
from __future__ import annotations

import datetime

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from awgbot.bot.callbacks import NoteCB, RsvCB, SetCB
from awgbot.bot.handlers.settings.reserve import move as move_h
from awgbot.bot.routers import make_dispatcher
from awgbot.core import config
from awgbot.domain.services.reserve import guest as guest_svc
from awgbot.runtime import pairlink
from tests.conftest import FakeBot
from tests.e2e.test_client_entry_points import _Session
from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import OWN_IP, PEER_IP

pytestmark = pytest.mark.e2e
rsv = rc.rsv
mv = rm.mv
ADMIN = config.ADMIN_ID


class _Bot(_Session):
    """Заглушка сессии, которая на отправку сообщения отвечает сообщением (как Telegram), на остальное — True."""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if type(method).__name__ == "SendMessage":
            return Message(message_id=900 + len(self.calls), date=datetime.datetime.now(),
                           chat=Chat(id=int(method.chat_id), type="private"), text=method.text)
        return True


class _Run:
    """Нажатия и сообщения админа через диспетчер роли."""

    def __init__(self, services, role="client"):
        self.dp = make_dispatcher(role, services, services.db, reattach=True)
        self.session = _Bot()
        self.bot = Bot("42:DUMMY", session=self.session)
        self.n = 0

    def _msg(self, **extra):
        self.n += 1
        return Message(message_id=100 + self.n, date=datetime.datetime.now(), chat=Chat(id=ADMIN, type="private"), **extra)

    async def press(self, cbdata) -> list:
        self.session.calls.clear()
        data = cbdata if isinstance(cbdata, str) else cbdata.pack()
        upd = Update(update_id=self.n + 1, callback_query=CallbackQuery(
            id=f"q{self.n}", chat_instance="ci", **{"from": User(id=ADMIN, is_bot=False, first_name="A")},
            message=self._msg(text="меню"), data=data))
        await self.dp.feed_update(self.bot, upd)
        return list(self.session.calls)

    async def say(self, text: str) -> list:
        self.session.calls.clear()
        upd = Update(update_id=self.n + 1, message=self._msg(text=text, from_user=User(id=ADMIN, is_bot=False, first_name="A")))
        await self.dp.feed_update(self.bot, upd)
        return list(self.session.calls)


def _kind(c) -> str:
    return type(c).__name__


def _edits(calls) -> list:
    return [c for c in calls if _kind(c) == "EditMessageText"]


def _labels(call) -> list[str]:
    mk = getattr(call, "reply_markup", None)
    return [b.text for row in mk.inline_keyboard for b in row] if mk else []


def _toast(calls):
    t = [c for c in calls if _kind(c) == "AnswerCallbackQuery"]
    return t[-1] if t else None


@pytest.fixture()
def ui(mv, make_active_client):
    make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    mv.run = _Run(mv.svc)
    return mv


async def _card(ui) -> tuple[str, list[str]]:
    e = _edits(await ui.run.press(SetCB(sec="rsv")))
    assert e, "карточка не показана"
    return e[0].text, _labels(e[0])


# ── карточка: кнопки по возможностям ────────────────────────────────────────

async def test_the_card_offers_the_move_when_it_is_possible_and_the_dns_screen_always(ui):
    text, labels = await _card(ui)
    assert "🔀 Переезд на «Beta»" in labels and "🌐 Запись DNS" in labels
    assert not any("Вернуть" in x for x in labels)
    assert "🌐 Запись <code>" not in text or rc.HOST_A in text


async def test_the_card_hides_the_move_button_and_says_why_in_the_text(ui):
    ui.svc.rsv_update("rsv_peer_state", session=False)
    text, labels = await _card(ui)
    assert not any("Переезд" in x for x in labels), "кнопка переезда при недоступном резерве"
    assert "Нет связи с" in text


async def test_the_card_offers_the_return_to_the_reserve_only_while_it_hosts_my_clients_and_is_reachable(ui):
    _text, labels = await _card(ui)
    assert not any("Вернуть всё на" in x for x in labels)
    ui.svc.reserve_set_in("serving", taken_at="2026-10-21T03:12:00+05:00")
    text, labels = await _card(ui)
    assert "↩️ Вернуть всё на «Beta»" in labels and not any("Переезд" in x for x in labels)
    assert "Пользователи «Beta»" in text and "здесь" in text
    ui.svc.rsv_update("rsv_peer_state", session=False)
    _text, labels = await _card(ui)
    assert not any("Вернуть всё на" in x for x in labels), "резерв не на связи — просить его вернуть клиентов некому"


async def test_the_card_of_the_primary_that_is_away_shows_the_state_and_no_move(ui):
    rm.go_away(ui.svc)
    text, labels = await _card(ui)
    assert "Мои пользователи: на «Beta»" in text.replace("<b>", "").replace("</b>", "") or "на <b>Beta</b>" in text or "Beta" in text
    assert not any("Переезд" in x for x in labels)


async def test_the_card_names_the_record_state(ui):
    ui.svc.reserve_dns_check()
    text, _labels_ = await _card(ui)
    assert "🌐 Запись" in text and rc.HOST_A in text and "вручную" in text


# ── диалог перевода ─────────────────────────────────────────────────────────

async def test_the_move_dialog_in_manual_mode_tells_the_human_what_to_change(ui):
    e = _edits(await ui.run.press(RsvCB(action="move")))
    assert e and "Смени у провайдера" in e[0].text and PEER_IP in e[0].text and rc.HOST_A in e[0].text
    assert _labels(e[0]) == ["⬅️ Отмена", "🔀 Перевести"], "«Отмена» первой: действие с последствиями"
    assert ui.svc.reserve_out() == "home", "диалог ничего не запускает"


async def test_the_move_dialog_in_automatic_mode_says_the_record_switches_itself(ui):
    rm.automatic(ui.svc)
    e = _edits(await ui.run.press(RsvCB(action="move")))
    assert "переключится" in e[0].text and "сама" in e[0].text and "Смени у провайдера" not in e[0].text


async def test_the_move_dialog_refuses_with_a_toast_when_the_move_is_impossible(ui):
    ui.svc.rsv_update("rsv_peer_state", session=False)
    calls = await ui.run.press(RsvCB(action="move"))
    t = _toast(calls)
    assert t.show_alert is True and "не на связи" in t.text and not _edits(calls)


async def test_confirming_the_move_starts_it_and_turns_the_dialog_into_the_live_message(ui):
    calls = await ui.run.press(RsvCB(action="move_yes"))
    assert ui.svc.reserve_out() == "leaving" and ui.svc.rsv_get("rsv_out")["step"] == "pkg"
    e = _edits(calls)
    assert e and "Перевод пользователей на" in e[0].text and "⏳" in e[0].text and not _labels(e[0])
    assert ui.svc.rsv_get("rsv_out")["msg"], "сообщение хода не запомнено — такты не смогут его править"


async def test_a_second_confirmation_of_the_move_is_refused(ui):
    await ui.run.press(RsvCB(action="move_yes"))
    t = _toast(await ui.run.press(RsvCB(action="move_yes")))
    assert t.show_alert is True and "уже не на этом сервере" in t.text


# ── живое сообщение хода ────────────────────────────────────────────────────

def _live_text(ui, step: str, **over) -> str:
    from awgbot.bot.texts.reserve import section as st_t
    rm.to_step(ui.svc, step)
    d = ui.svc.reserve_move_live()
    d.update(over)
    return st_t.move_live(d)


@pytest.mark.parametrize("step,done", [("pkg", 0), ("eph", 1), ("up", 2), ("record", 3), ("release", 4), ("released", 5)])
def test_the_live_message_checks_off_the_steps_in_order(ui, step, done):
    """Шесть пунктов живого сообщения отмечаются по порядку: данные, ключ,
    подъём, запись, бот. Сбой счёта — человек видит «готово» у шага, который
    ещё идёт, или застрявший ⏳ у пройденного."""
    text = _live_text(ui, step)
    assert text.count("✅") == done, text
    assert "⏳ <b>Перевод пользователей на" in text


@pytest.mark.parametrize("step,line", [
    ("pkg", "⏳ передаю зашифрованный ключ переезда…"),
    ("eph", "⏳ передаю зашифрованный ключ переезда…"),
    ("up", "✅ зашифрованный ключ переезда передан"),
    ("released", "✅ зашифрованный ключ переезда передан"),
])
def test_the_live_message_shows_the_key_step_as_its_own_line(ui, step, line):
    """Передача ключа переезда — отдельный пункт между данными и подъёмом:
    пока резерв ждёт конверт, человек видит, что ждём именно ключ, а не
    подъёма профилей; после — пункт отмечен."""
    lines = _live_text(ui, step).split("\n")
    assert line in lines, lines
    i = lines.index(line)
    assert lines[i - 1].split(" ", 1)[1].startswith("данные для переезда"), f"пункт ключа не после данных: {lines}"
    assert lines[i + 1].endswith("поднял профили"), f"пункт ключа не перед подъёмом: {lines}"


def test_the_card_at_the_ephemeral_key_step_says_it_waits_for_the_clients(ui):
    """Карточка резерва на шаге `eph` — та же фраза, что на подъёме: шаг ключа
    человеку служебным словом не показывается."""
    from awgbot.bot.texts.reserve.section import card_text
    rm.to_step(ui.svc, "eph")
    text = card_text(ui.svc.reserve_card_data())
    assert "переезжают на «Beta» — передаю зашифрованный ключ переезда" in text, text
    assert "идёт переезд" not in text, "шаг ключа карточке неизвестен — показан общей фразой"


def test_the_live_message_in_manual_mode_asks_for_the_record_change_at_the_record_step(ui):
    text = _live_text(ui, "record")
    assert "смени запись" in text and rc.HOST_A in text and PEER_IP in text


def test_the_live_message_in_automatic_mode_only_waits(ui):
    rm.automatic(ui.svc)
    text = _live_text(ui, "record")
    assert "жду смену записи" in text and "смени запись" not in text


def test_the_live_message_summarizes_success_and_failure(ui):
    away = _live_text(ui, "released", state="away", online=41)
    assert away.startswith("✅ Пользователи и бот — на") and "ещё 41" in away
    failed = _live_text(ui, "pkg", state="home", reason="запись не изменена — HTTP 403")
    assert failed == "⚠️ Перевод не состоялся: запись не изменена — HTTP 403"


async def test_the_tick_edits_the_live_message_and_forgets_it_when_the_move_has_ended(ui):
    svc = ui.svc
    bot = FakeBot()
    rm.to_step(svc, "record")
    svc.rsv_update("rsv_out", msg=[ADMIN, 555])
    await move_h.refresh_live(bot, svc)
    edits = [r for r in bot.records if r[0] == "edit_message_text"]
    assert len(edits) == 1 and edits[0][1] == ADMIN and "Перевод пользователей на" in edits[0][2]
    assert svc.rsv_get("rsv_out")["msg"] == [ADMIN, 555], "ход ещё идёт: сообщение держим"
    svc.reserve_set_out("away")
    await move_h.refresh_live(bot, svc)
    assert svc.rsv_get("rsv_out")["msg"] == [], "ход закончен, а сообщение продолжают править"


async def test_the_tick_without_a_live_message_edits_nothing(ui):
    bot = FakeBot()
    await move_h.refresh_live(bot, ui.svc)
    assert bot.records == []


async def test_an_edit_that_telegram_rejects_does_not_break_the_tick(ui):
    class Bad(FakeBot):
        async def edit_message_text(self, *a, **kw):
            raise RuntimeError("message is not modified")
    rm.to_step(ui.svc, "up")
    ui.svc.rsv_update("rsv_out", msg=[ADMIN, 555])
    await move_h.refresh_live(Bad(), ui.svc)


# ── вернуть клиентов к основе (резерв) ──────────────────────────────────────

async def test_the_evict_dialog_and_its_confirmation_send_the_request_to_the_primary(ui, monkeypatch):
    ui.svc.reserve_set_in("serving", taken_at=rm.timeutil.now_iso())
    sent = []

    async def send(kind, body=None):
        sent.append(kind)
        return True
    monkeypatch.setattr(pairlink, "send", send)
    e = _edits(await ui.run.press(RsvCB(action="evict")))
    assert "Вернуть пользователей и бот" in e[0].text and _labels(e[0]) == ["⬅️ Отмена", "↩️ Вернуть"]
    assert sent == [], "диалог сам ничего не просит"
    calls = await ui.run.press(RsvCB(action="evict_yes"))
    assert sent == ["evict"]
    assert _toast(calls).text == "Просьба о возврате отправлена"


async def test_the_evict_is_refused_with_a_toast_when_nothing_is_hosted(ui, monkeypatch):
    sent = []

    async def send(kind, body=None):
        sent.append(kind)
        return True
    monkeypatch.setattr(pairlink, "send", send)
    for act in ("evict", "evict_yes"):
        t = _toast(await ui.run.press(RsvCB(action=act)))
        assert t.show_alert is True and "здесь нет" in t.text
    assert sent == []


# ── запись DNS ──────────────────────────────────────────────────────────────

async def test_the_dns_screen_without_a_name_says_the_record_does_not_exist(ui):
    from awgbot.core import settings
    settings.set_value("app.network.server_host", "203.0.113.5")
    e = _edits(await ui.run.press(RsvCB(action="dns")))
    assert "Адрес для пользователей — IP" in e[0].text and "без записи переезд невозможен" in e[0].text
    assert _labels(e[0]) == ["⬅️ Назад"], "кнопок провайдера и токена у IP быть не должно"


async def test_the_dns_screen_in_manual_mode_has_no_token_button(ui):
    e = _edits(await ui.run.press(RsvCB(action="dns")))
    assert rc.HOST_A in e[0].text and "Провайдер: вручную" in e[0].text
    assert _labels(e[0]) == ["🏷 Провайдер: вручную", "🧪 Проверить", "⬅️ Назад"]


async def test_the_provider_button_cycles_through_all_providers(ui):
    seen = []
    for _ in range(6):
        e = _edits(await ui.run.press(RsvCB(action="dns_prov")))
        seen.append(ui.svc.reserve_dns_provider())
        assert any(x.startswith("🏷 Провайдер: ") for x in _labels(e[0]))
    assert seen == ["cloudflare", "duckdns", "dyndns2", "digitalplat", "manual", "cloudflare"]


async def test_the_digitalplat_key_prompt_names_the_key_prefix_and_scopes(ui):
    """Без подсказки про `dp_live_` и области dns.read/dns.write человек
    принесёт ключ без права записи — переезд упадёт на смене записи."""
    ui.svc.reserve_dns_set_provider("digitalplat")
    e = _edits(await ui.run.press(RsvCB(action="dns_token")))
    assert "Ключ DigitalPlat" in e[0].text and "сообщение удалю" in e[0].text
    assert "dp_live_" in e[0].text and "dns.read" in e[0].text and "dns.write" in e[0].text


async def test_the_token_is_asked_deleted_from_the_chat_and_stored(ui):
    ui.svc.reserve_dns_set_provider("cloudflare")
    e = _edits(await ui.run.press(RsvCB(action="dns_token")))
    assert "Токен Cloudflare" in e[0].text and "сообщение удалю" in e[0].text and "example.org" in e[0].text
    calls = await ui.run.say("DUMMY-cf-token")
    assert "DeleteMessage" in [_kind(c) for c in calls], "токен остался в переписке"
    assert ui.svc.rsv_get("rsv_dns")["token"] == "DUMMY-cf-token" and ui.svc.reserve_dns_automatic()
    shown = " ".join(getattr(c, "text", "") or "" for c in calls)
    assert "DUMMY-cf-token" not in shown, "токен повторён в ответе бота"
    assert "Запись" in shown


async def test_a_bad_token_is_refused_and_the_dialog_keeps_waiting(ui):
    ui.svc.reserve_dns_set_provider("duckdns")
    await ui.run.press(RsvCB(action="dns_token"))
    calls = await ui.run.say("два слова")
    shown = " ".join(getattr(c, "text", "") or "" for c in calls)
    assert "не похоже на токен" in shown and not ui.svc.reserve_dns_automatic()
    assert "DeleteMessage" in [_kind(c) for c in calls], "даже негодный токен не должен оставаться в чате"
    calls = await ui.run.say("DUMMY-duck-token")
    assert ui.svc.reserve_dns_automatic(), "после ошибки диалог не принял верный токен"


async def test_the_token_button_is_refused_in_manual_mode(ui):
    t = _toast(await ui.run.press(RsvCB(action="dns_token")))
    assert t.show_alert is True and "токен не нужен" in t.text


async def test_the_check_button_shows_the_result_in_a_toast(ui):
    calls = await ui.run.press(RsvCB(action="dns_check"))
    t = _toast(calls)
    assert t.text.startswith("✅") and OWN_IP in t.text and not t.show_alert
    ui.dns.auth_error = True
    t = _toast(await ui.run.press(RsvCB(action="dns_check")))
    assert t.text.startswith("⚠️") and t.show_alert is True


# ── главная: строка Т-Р17 ───────────────────────────────────────────────────

async def test_the_admin_main_screen_shows_the_reserve_line_in_the_ordinary_and_the_unusual_state(ui):
    """Обычное состояние при паре — строка в стиле строки сервера: имя, кружок,
    суть; переезд — та же строка с другой сутью, а не вторая строка рядом."""
    from awgbot.bot.handlers.admin import panel
    parts = await panel._panel_parts(ui.svc)
    assert "🛟 <b>Beta</b> 🟡 не готов: данных для переезда ещё нет" in parts[0], parts[0]
    rm.confirm_package(ui.svc)
    parts = await panel._panel_parts(ui.svc)
    assert "🛟 <b>Beta</b> 🟢 резерв готов · данные от " in parts[0], "строки резерва нет на главной в обычном состоянии"
    ui.svc.reserve_set_in("serving", taken_at="2026-10-21T03:12:00+05:00")
    parts = await panel._panel_parts(ui.svc)
    assert "🛟 <b>Beta</b> 🟢 чужие пользователи и бот у тебя с" in parts[0] and "резерв готов" not in parts[0]
    assert parts[0].count("🛟") == 1, "строка резерва на главной должна быть одна"


def test_the_reserve_status_on_the_main_screen_escapes_the_name_and_links_to_the_card():
    """Имя второй стороны приходит из сопряжения — чужой текст: без
    экранирования «<b>» ломает разметку всей главной (Telegram отвергает
    сообщение). Строка — ссылкой на карточку резерва; без имени бота — текст;
    статус важнее запасной строки."""
    from awgbot.bot import texts
    from awgbot.bot.texts.admin.panel import admin_panel
    st = {"ok": True}
    rsv = {"name": "<b>Beta</b>", "dot": "🟢", "text": "резерв готов · a & b"}
    text = admin_panel(st, bot_username="srv_a_bot", rsv=rsv, rsv_line="🛟 запасная")
    assert f'<a href="https://t.me/srv_a_bot?start={texts.RSV_PAYLOAD}">🛟 <b>&lt;b&gt;Beta&lt;/b&gt;</b> 🟢 ' \
           "резерв готов · a &amp; b</a>" in text, text
    assert "запасная" not in text, "при статусе запасная строка не рисуется"
    plain = admin_panel(st, rsv={"name": "Beta", "dot": "🔴", "text": "недоступен с 13:58"})
    assert "href" not in plain and "🛟 <b>Beta</b> 🔴 недоступен с 13:58" in plain, plain
    assert "🛟" not in admin_panel(st, bot_username="srv_a_bot", rsv=None), "без пары строки резерва нет"


def test_the_reserve_line_on_the_main_screen_links_to_the_reserve_card():
    """Строка Т-Р17 — ссылкой `/start rsv` на карточку резерва, имя второй
    стороны экранировано; без имени бота — просто текст."""
    from awgbot.bot import texts
    from awgbot.bot.handlers.admin.panel import parse_link
    from awgbot.bot.texts.admin.panel import admin_panel
    st = {"ok": True}
    text = admin_panel(st, bot_username="srv_a_bot", rsv_line="🛟 У тебя клиенты и бот «<b>Beta</b>»")
    assert f'<a href="https://t.me/srv_a_bot?start={texts.RSV_PAYLOAD}">' in text, text
    assert "&lt;b&gt;Beta&lt;/b&gt;" in text and "<b>Beta</b>" not in text, "имя второй стороны не экранировано"
    plain = admin_panel(st, rsv_line="🛟 У тебя клиенты и бот «Beta»")
    assert "href" not in plain and "🛟 У тебя клиенты и бот «Beta»" in plain
    assert parse_link(texts.RSV_PAYLOAD) == ("set_rsv", 0)


# ── уведомление Т-РУ16: «Вернуть запись» ────────────────────────────────────

def test_the_record_notice_carries_the_return_button():
    from awgbot.bot import notifier
    mk = notifier.action_markup(("rsv_record_back", 0))
    labels = [b.text for row in mk.inline_keyboard for b in row]
    assert labels[0] == "↩️ Вернуть запись" and labels[-1] != labels[0]


async def test_the_return_button_switches_the_record_and_answers(ui):
    rm.automatic(ui.svc)
    calls = await ui.run.press(NoteCB(kind="rsv_record_back", ref=0))
    assert ui.dns.sets == [OWN_IP]
    t = _toast(calls)
    assert t.text == "Запись возвращена на этот сервер" and not t.show_alert


async def test_the_return_button_reports_a_refusal_loudly(ui):
    rm.automatic(ui.svc)
    ui.dns.set_error = "HTTP 403"
    t = _toast(await ui.run.press(NoteCB(kind="rsv_record_back", ref=0)))
    assert t.show_alert is True and "403" in t.text and t.text.startswith("Запись не изменена")


# ── кнопки возврата: чьи хендлеры ловят нажатие ─────────────────────────────

async def test_the_return_button_of_the_primary_is_handled_by_the_primarys_handler(ui):
    """В основном боте «↩️ Вернуть клиентов» (рычаг «по кнопке») открывает диалог возврата: нажатие
    не должно уходить хендлеру гостевого режима, который в этой роли молчит."""
    rm.go_away(ui.svc)
    ui.svc.rsv_update("rsv_out", can_return=True)
    calls = await ui.run.press(RsvCB(action="back"))
    assert _edits(calls), f"нажатие «Вернуть клиентов» без реакции: {[_kind(c) for c in calls]}"
    calls = await ui.run.press(RsvCB(action="back_yes"))
    assert ui.svc.reserve_out() == "returning", "подтверждение возврата проглочено чужим хендлером"


# ── бот основы на резерве ───────────────────────────────────────────────────

@pytest.fixture()
def gui(mv, make_active_client, monkeypatch):
    monkeypatch.setattr(config, "GUEST", True)
    make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    mv.run = _Run(mv.svc, "guest")
    return mv


@pytest.mark.parametrize("act", ["move", "move_yes", "evict", "evict_yes", "dns", "dns_prov", "dns_token", "dns_check"])
async def test_the_move_and_dns_buttons_do_not_work_in_the_bot_on_the_reserve(gui, act):
    calls = await gui.run.press(RsvCB(action=act))
    t = _toast(calls)
    assert [_kind(c) for c in calls] == ["AnswerCallbackQuery"] and t.text == guest_svc.GUEST_REFUSED


async def test_the_guest_return_dialog_opens_only_when_the_primary_said_it_waits_for_the_button(gui):
    gui.svc.guest_state_in({"name": "Бета", "own_name": "Альфа", "primary": {"status": "alive", "can_return": False},
                            "return": "button"})
    t = _toast(await gui.run.press(RsvCB(action="back")))
    assert t.show_alert is True and "ещё не в строю" in t.text
    gui.svc.guest_state_in({"name": "Бета", "own_name": "Альфа", "since": "2026-10-21T03:12:00+05:00",
                            "primary": {"status": "alive", "healthy": True, "can_return": True,
                                        "since": "2026-10-21T09:40:00+05:00"}, "return": "button"})
    e = _edits(await gui.run.press(RsvCB(action="back")))
    assert e and "Вернуть пользователей на" in e[0].text and "Альфа" in e[0].text and "09:40" in e[0].text
    assert _labels(e[0]) == ["⬅️ Отмена", "↩️ Вернуть"]


async def test_the_guest_return_confirmation_sends_back_req_over_the_local_channel(gui, monkeypatch):
    from awgbot.runtime.pairlink import guestctl
    sent = []

    class Client:
        async def send(self, kind, body=None):
            sent.append(kind)
            return True
    monkeypatch.setattr(guestctl, "client", lambda: Client())
    gui.svc.guest_state_in({"name": "Бета", "own_name": "Альфа", "primary": {"can_return": True}, "return": "button"})
    t = _toast(await gui.run.press(RsvCB(action="back_yes")))
    assert sent == ["back_req"] and t.text == "Просьба о возврате отправлена"


async def test_the_guest_return_confirmation_without_the_channel_says_so(gui, monkeypatch):
    from awgbot.runtime.pairlink import guestctl
    monkeypatch.setattr(guestctl, "client", lambda: None)
    t = _toast(await gui.run.press(RsvCB(action="back_yes")))
    assert t.show_alert is True and "Нет связи с ботом резерва" in t.text


async def test_the_guest_return_dialog_says_which_record_goes_back(gui):
    """Т-Р11: «запись <code>vpn…</code> вернётся на <code>…</code>» — админ должен знать, что запись
    поменяется. Текст диалога умеет эту фразу, но состояние гостя не несёт ни имени записи, ни адреса."""
    gui.svc.guest_state_in({"name": "Бета", "own_name": "Альфа", "since": "2026-10-21T03:12:00+05:00",
                            "primary": {"status": "alive", "healthy": True, "can_return": True,
                                        "since": "2026-10-21T09:40:00+05:00"}, "return": "button"})
    e = _edits(await gui.run.press(RsvCB(action="back")))
    assert rc.HOST_A in e[0].text, e[0].text
