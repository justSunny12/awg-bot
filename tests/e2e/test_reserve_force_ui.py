"""С9 в карточке основы: «⚠️ Вернуть пользователей без «Beta»» — кнопка только
когда рычаг возможен, диалог с «Отменой» первой и опасной кнопкой, тексты
по режиму записи, подтверждение запускает рычаг; строка «⏳ жду реплику» в
карточке, пока реплика резерва не влилась; в госте кнопки
не работают. Вторая проверка рычага — словом Telegram (getWebhookInfo, вебхук
не снимается): вебхук стоит, очередь мала и свежей ошибки доставки нет — бот на
резерве отвечает, рычаг отказывает (рвётся только линк пары); Telegram не
ответил — рычаг идёт. Гость, узнавший из надзора, что вебхук не у него, решений
больше не пишет (ограждение базы, кворум его не снимает) и говорит боту
резерва — тот админу резерва один раз; адрес снова его — ограждение снято.

Нажатия идут через настоящий диспетчер роли (`_Run` из test_reserve_move_ui).

Цена ошибки: кнопка при живом резерве — админ отбирает Telegram у бота,
который ещё обслуживает пользователей; диалог без слов о записи — человек не
знает, что менять руками; рычаг в госте — гость отбирает
Telegram сам у себя; рычаг при живом вебхуке гостя за разорванным линком — два
бота на один токен, решения пишут обе стороны; гость, пишущий решения после
того, как основа забрала Telegram, — его правки расходятся с основой, а админ
резерва узнаёт об этом только из журнала.
"""
from __future__ import annotations

import time
import types

import pytest
from aiogram.types import WebhookInfo

from awgbot.bot.callbacks import RsvCB
from awgbot.bot.texts.reserve import section as st_t
from awgbot.domain.services.reserve import guest as guest_svc
from awgbot.domain.services.reserve import outbound_return
from awgbot.util import timeutil
from tests.e2e.test_reserve_move_ui import _card, _edits, _kind, _labels, _toast, gui, ui
from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import OWN_IP

pytestmark = pytest.mark.e2e
rsv = rc.rsv
mv = rm.mv
__all__ = ["ui", "gui"]

FORCE = "⚠️ Вернуть без «Beta»"


def _ago(minutes: float) -> str:
    return timeutil.to_iso(timeutil.shift(timeutil.now(), -minutes * 60))


def _gone(ui, minutes: float = 11) -> None:
    rm.go_away(ui.svc, kind="dead")
    ui.svc.db.set_state("last_server_ok", "0")
    ui.svc.rsv_update("rsv_peer_state", session=False, down_since=_ago(minutes))


async def test_the_card_offers_the_lever_only_when_the_reserve_is_gone_ten_minutes(ui):
    _text, labels = await _card(ui)
    assert FORCE not in labels, "рычаг, когда пользователи и так здесь"
    _gone(ui, minutes=5)
    _text, labels = await _card(ui)
    assert FORCE not in labels, "рычаг раньше 10 минут"
    _gone(ui)
    _text, labels = await _card(ui)
    assert FORCE in labels, labels
    ui.svc.rsv_update("rsv_peer_state", session=True)
    _text, labels = await _card(ui)
    assert FORCE not in labels, "рычаг при резерве на связи"


async def test_the_dialog_in_manual_mode_tells_what_to_change_and_puts_cancel_first(ui):
    _gone(ui)
    e = _edits(await ui.run.press(RsvCB(action="force")))
    assert e, "диалог не показан"
    text = e[0].text
    assert "Вернуть пользователей без «Beta»?" in text and "смени у провайдера запись" in text and OWN_IP in text, text
    assert rc.HOST_A in text and "Пока «Beta» не на связи, пересылки устройств с него нет" in text, text
    assert _labels(e[0]) == ["⬅️ Отмена", "⚠️ Да, вернуть"], _labels(e[0])
    assert not ui.svc.rsv_get("rsv_out").get("await_merge"), "диалог сам запустил рычаг"


async def test_the_dialog_in_automatic_mode_says_the_record_returns_by_itself(ui):
    _gone(ui)
    rm.automatic(ui.svc)
    text = _edits(await ui.run.press(RsvCB(action="force")))[0].text
    assert "вернётся на" in text and "смени у провайдера" not in text, text


async def test_the_dialog_is_refused_with_the_reason_when_the_lever_is_impossible(ui):
    calls = await ui.run.press(RsvCB(action="force"))
    t = _toast(calls)
    assert t.show_alert and "не у резерва" in t.text and not _edits(calls)


async def test_confirming_runs_the_lever_and_redraws_the_card(ui):
    _gone(ui)
    rm.automatic(ui.svc)
    calls = await ui.run.press(RsvCB(action="force_yes"))
    assert ui.svc.rsv_get("rsv_out").get("await_merge") is True and ui.dns.sets == [OWN_IP]
    assert _toast(calls).text == "Запись возвращается, Telegram забираю"
    assert _edits(calls), "карточка не перерисована"


async def test_a_confirmation_that_came_too_late_is_refused_loudly(ui):
    _gone(ui)
    ui.svc.rsv_update("rsv_peer_state", session=True)          # резерв вышел на связь, пока диалог висел
    ui.svc._lv.state = "ok"                                     # живым словом, не одним флагом сеанса
    t = _toast(await ui.run.press(RsvCB(action="force_yes")))
    assert t.show_alert and "на связи" in t.text
    assert not ui.svc.rsv_get("rsv_out").get("await_merge")


async def test_the_card_says_it_waits_for_the_replica_while_the_mark_stands(ui):
    ui.svc.reserve_set_out("home", await_merge=True, kind="dead")
    text, labels = await _card(ui)
    assert "⏳ жду реплику «Beta»" in text and "изменения из неё применятся" in text, text
    assert FORCE not in labels
    ui.svc.reserve_set_out("home", await_merge=False)
    text, _labels_ = await _card(ui)
    assert "жду реплику" not in text


def test_the_dialog_text_without_a_record_has_no_record_sentence():
    text = st_t.force_dialog({"name": "Beta", "since": "", "record": "", "own": "", "manual": True})
    assert "запись" not in text and "«Beta» не на связи. " in text, text
    assert not text.rstrip().endswith(".")


@pytest.mark.parametrize("act", ["force", "force_yes"])
async def test_the_lever_does_not_work_in_the_bot_on_the_reserve(gui, act):
    calls = await gui.run.press(RsvCB(action=act))
    assert [_kind(c) for c in calls] == ["AnswerCallbackQuery"] and _toast(calls).text == guest_svc.GUEST_REFUSED


# ── вторая проверка рычага: слово Telegram ──────────────────────────────────

HOOK = "https://203.0.113.9:8443/DUMMY"


def _telegram(ui, *, url: str = HOOK, pending: int = 0, err_ago: float | None = None, fail: bool = False) -> list:
    """getWebhookInfo в сессии бота основы отвечает так; возвращает список его вызовов."""
    asked: list = []
    plain = ui.run.session.make_request

    async def make_request(bot, method, timeout=None):
        if type(method).__name__ != "GetWebhookInfo":
            return await plain(bot, method, timeout)
        asked.append(method)
        ui.run.session.calls.append(method)
        if fail:
            raise RuntimeError("Telegram не ответил")
        return WebhookInfo(url=url, has_custom_certificate=bool(url), pending_update_count=pending,
                           last_error_date=None if err_ago is None else int(time.time() - err_ago),
                           last_error_message=None if err_ago is None else "Connection timed out")

    ui.run.session.make_request = make_request
    return asked


async def test_the_lever_is_refused_while_the_bot_on_the_reserve_answers_in_telegram(ui):
    _gone(ui)
    rm.automatic(ui.svc)
    asked = _telegram(ui, pending=1)
    calls = await ui.run.press(RsvCB(action="force_yes"))
    t = _toast(calls)
    assert asked, "рычаг не спросил Telegram"
    assert t.show_alert and t.text == "Бот на «Beta» отвечает в Telegram — верни пользователей обычным путём", t.text
    assert not ui.svc.rsv_get("rsv_out").get("await_merge") and ui.dns.sets == [], "рычаг пошёл при живом вебхуке"


@pytest.mark.parametrize("hook", [
    {"pending": 12, "err_ago": 120},                       # очередь растёт, доставка падает — резерв мёртв
    {"pending": 0, "err_ago": 60},                         # свежая ошибка доставки
    {"url": ""},                                           # вебхука нет
    {"fail": True},                                        # Telegram не ответил — рычаг аварийный
], ids=["queue", "error", "no-hook", "no-answer"])
async def test_the_lever_goes_when_telegram_does_not_say_the_reserve_answers(ui, hook):
    _gone(ui)
    rm.automatic(ui.svc)
    _telegram(ui, **hook)
    calls = await ui.run.press(RsvCB(action="force_yes"))
    assert _toast(calls).text == "Запись возвращается, Telegram забираю", _toast(calls).text
    assert ui.svc.rsv_get("rsv_out").get("await_merge") is True


async def test_an_impossible_lever_does_not_ask_telegram(ui):
    asked = _telegram(ui)
    t = _toast(await ui.run.press(RsvCB(action="force_yes")))
    assert t.show_alert and "не у резерва" in t.text and asked == []


@pytest.mark.parametrize("info, answers", [
    ({"url": HOOK, "pending_update_count": 0}, True),
    ({"url": HOOK, "pending_update_count": outbound_return.FORCE_HOOK_PENDING_MAX}, True),
    ({"url": HOOK, "pending_update_count": outbound_return.FORCE_HOOK_PENDING_MAX + 1}, False),
    ({"url": HOOK, "pending_update_count": 0, "last_error_date": 1_000_000 - 60}, False),
    ({"url": HOOK, "pending_update_count": 0, "last_error_date": 1_000_000 - outbound_return.FORCE_HOOK_ERR_S - 1}, True),
    ({"url": "", "pending_update_count": 0}, False),
    ({"url": HOOK, "pending_update_count": "много"}, False),
])
def test_the_webhook_verdict(info, answers):
    assert outbound_return.webhook_answers(info, 1_000_000) is answers


# ── гость: вебхук не у него ─────────────────────────────────────────────────

def test_a_foreign_webhook_stops_the_decisions_of_the_guest_until_it_is_mine_again(services):
    assert not services.reserve_following()
    services.guest_webhook_foreign()
    m = services.db.jrn_meta()
    assert (m["holder"], m["role"]) == ("self", "follower"), f"гость пишет решения при чужом вебхуке: {m}"
    assert services.reserve_following(), "задания решений гостя не выйдут"
    ld = types.SimpleNamespace(yes_count=lambda now: 2, v=2)
    assert services.reserve_quorum_fence_apply("unfence", ld) == []
    assert services.db.jrn_meta()["role"] == "follower", "«да» кворума вернули гостю право записи при вебхуке у основы"
    services.guest_webhook_foreign()                         # следующий надзор — то же, без второго перехода
    assert services.db.jrn_meta()["role"] == "follower"
    services.guest_webhook_ours()
    assert services.db.jrn_meta()["role"] == "holder" and not services.reserve_following()


def test_the_reserve_admin_is_told_once_that_the_webhook_left_the_guest(services, rsv):
    rc.pair_row(services)
    notes = [n for a in services.reserve_guest_webhook_in({"foreign": True, "stalled": False}) if a[0] == "notify"
             for n in a[1]]
    assert len(notes) == 1 and notes[0].force_sound, notes
    assert notes[0].text == ("⚠️ Вебхук бота «Beta» указывает не на этот сервер — гостевой бот "
                             "здесь решения больше не принимает"), notes[0].text
    assert services.reserve_guest_webhook_in({"foreign": True, "stalled": False}) == [], "повтор на каждом надзоре"


async def test_the_guest_watch_fences_on_a_foreign_webhook_and_lifts_when_it_is_back(services, fake_bot):
    from awgbot.runtime.main import guest as guest_main

    class Wh:
        def __init__(self):
            self.url_ours = None
            self.next = {}

        async def watch(self, bot):
            self.url_ours = not self.next.get("foreign")
            return self.next

    class Ctl:
        def __init__(self):
            self.sent = []

        async def send(self, kind, body=None):
            self.sent.append((kind, body))
            return True

    wh, ctl = Wh(), Ctl()
    wh.next = {"foreign": True, "url": ""}
    await guest_main._watch(wh, fake_bot, services, ctl)
    await guest_main._watch(wh, fake_bot, services, ctl)
    assert services.reserve_following() and ctl.sent == [("webhook", {"foreign": True, "url": ""})] * 2, ctl.sent
    wh.next = {}
    await guest_main._watch(wh, fake_bot, services, ctl)
    assert not services.reserve_following(), "адрес снова мой — ограждение не снято"
