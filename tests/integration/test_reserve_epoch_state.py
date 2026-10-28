"""Роль эпохи у основы — по состоянию переезда, а не по слову Telegram
(domain/services/reserve/journal/epoch.py, правило `_follow_wanted`): правило
сверяется при каждой смене `rsv_out` и `rsv_poll`, на каждом шаге автомата и на
такте. Здесь — сценарии, в которых роль по одному слову Telegram выходила
неверной: вебхук, оставшийся у остановленного гостя или у резерва без гостя,
отказ слияния, сбой отправки финальной реплики, окно слияния, две отметки в
одну секунду, «вернуть без резерва», отметки прежнего переезда; а ещё — что
следующий пишет итог проверки ящика и что переход эпохи не собирает пакет.

Мир — пара из tests/integration/_reserve_move.py; Bot API у поллинга —
заглушка с ответом getWebhookInfo; реплика бота основы на резерве — копия
базы основы (`snapshot_blob`). Адреса — документационные, ключи — заглушки.

Цена ошибки: следование там, где писать решения больше некому (гость
остановлен, слияние не прошло, реплика не ушла), — пользователи и бот снова
у основы, а каждое действие с профилями получает отказ «пользователи на
резерве», и перезапуск этого не лечит (роль живёт в базе); держатель там,
где пишет гость, — основа по устаревшей базе истекает продлённые на резерве
подписки, а слияние закрепляет это; эпоха, мигающая на каждом слове
Telegram, — лишние переходы с synchronous = FULL и лишние пакеты переезда
резерву; отказ записи итога проверки ящика — запасной канал критичных
алертов падает именно тогда, когда Telegram недоступен.
"""
from __future__ import annotations

import sqlite3
import types

import pytest

from awgbot.infra import mail
from awgbot.runtime import pollctl, rsvactions
from awgbot.runtime.scheduler import setup_scheduler
from awgbot.util import rsvjrn, timeutil
from tests.conftest import restore_settings

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration.test_reserve_actions import act
from tests.integration.test_reserve_follow import world

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["act", "world"]

HOOK = "https://backup.example.net:8443/hook"           # вебхук бота основы на резерве


def _following(svc) -> bool:
    return svc.db.jrn_following()


def _epoch(svc) -> int:
    return int(svc.db.jrn_meta()["epoch"])


class _Bot:
    """Bot API глазами поллинга: только getWebhookInfo."""

    def __init__(self, url: str = ""):
        self.url = url
        self.session = None

    async def get_webhook_info(self):
        return types.SimpleNamespace(url=self.url)


async def _telegram_says(svc, url: str) -> bool:
    """Поллинг спросил getWebhookInfo; True — можно поллить."""
    return await pollctl.PollControl(None, _Bot(url), svc)._allowed()


# ── 1. возврат со слиянием и перезапуск: вебхук у остановленного гостя ──────

async def test_after_the_merge_and_restart_a_webhook_at_the_stopped_guest_does_not_take_the_epoch(world):
    """Реплика возврата пришла и слита, бот перезапущен; Telegram ещё говорит
    «вебхук стоит» — это вебхук остановленного гостя. Основа держит эпоху на
    take_tg, record_back и relay и до конца возврата; переход — ровно один (по
    окончании слияния), без миганий на каждом слове Telegram."""
    svc = world.svc
    svc.reserve_return_start("button")
    assert svc.reserve_released_in(rm.snapshot_blob(svc, gen=7)) == [("restart",)], \
        svc.rsv_get("rsv_out").get("merge_error")
    assert not _following(svc), "слияние прошло — решения всё ещё у остановленного гостя"
    epoch = _epoch(svc)
    assert svc.reserve_return_after_restart() is True, "после перезапуска возврат не перешёл к take_tg"
    assert await _telegram_says(svc, HOOK) is False
    assert svc.reserve_poll() == "away"
    assert not _following(svc), "take_tg: вебхук остановленного гостя увёл эпоху к резерву"
    assert ("take_tg",) in svc.reserve_out_tick()
    assert not _following(svc), "take_tg: такт увёл эпоху по слову Telegram"
    assert svc.reserve_tg_taken_mark(), "Telegram забран, а шага record_back нет"
    await _telegram_says(svc, HOOK)                        # ответ, опоздавший к deleteWebhook
    assert svc.rsv_get("rsv_out")["step"] == "record_back" and not _following(svc), \
        "record_back: запоздалое слово Telegram увело эпоху"
    svc.reserve_out_tick()                                 # запись уже на мне — сразу relay
    assert svc.rsv_get("rsv_out")["step"] == "relay", svc.rsv_get("rsv_out")["step"]
    await _telegram_says(svc, HOOK)
    svc.reserve_out_tick()
    assert not _following(svc), "relay: основа следует за остановленным гостем"
    svc.reserve_wiped_in()
    assert svc.reserve_out() == "home" and not _following(svc)
    assert _epoch(svc) == epoch, f"эпоха мигала после слияния: {epoch} → {_epoch(svc)}"


def test_starting_a_return_does_not_hand_the_epoch_back_for_a_moment(world):
    """Начало возврата (away → returning/back_req) при живом госте: эпоха остаётся у
    резерва, без промежуточного «держатель здесь». Иначе — два лишних перехода с
    synchronous = FULL на каждый возврат и окно, в котором задания решений другого
    потока основы пишут по устаревшей базе, пока гость ещё отвечает людям."""
    svc = world.svc
    epoch = _epoch(svc)
    assert _following(svc)
    svc.reserve_return_start("button")
    assert svc.rsv_get("rsv_out")["step"] == "back_req" and _following(svc)
    assert _epoch(svc) == epoch, f"начало возврата сдвинуло эпоху: {epoch} → {_epoch(svc)} ({svc.db.jrn_meta()['why']})"


# ── 2. зависший вебхук без гостя: срыв переезда после released ──────────────

def _failed_after_release(svc) -> None:
    """Переезд дошёл до передачи бота и сорвался: дома, вебхук остался у резерва."""
    rm.to_step(svc, "release", gen=7)
    assert svc.reserve_release_build() is not None
    assert _following(svc), "финальная реплика собрана — а эпоха не ушла (сценарий не о том)"
    svc._move_fail("резерв не взял право отвечать — вебхук не поставлен", released=True)
    assert svc.reserve_out() == "home" and not _following(svc), "срыв переезда — эпоха не вернулась"


async def test_a_webhook_left_after_a_failed_move_is_not_a_holder_while_the_reserve_says_idle(mv):
    svc = mv.svc
    _failed_after_release(svc)
    rm.st_in(svc, **{"in": "idle"})                        # резерв на связи: зону снял, гостя нет
    epoch = _epoch(svc)
    assert await _telegram_says(svc, HOOK) is False
    assert svc.reserve_poll() == "away"
    assert not _following(svc), "вебхук у резерва без гостя увёл эпоху — решения не пишет никто"
    svc.reserve_out_tick()
    assert not _following(svc) and _epoch(svc) == epoch, "такт увёл эпоху по остатку вебхука"


async def test_a_webhook_left_after_a_failed_move_is_followed_while_the_reserve_is_silent_and_dropped_when_it_says_idle(mv):
    """Резерв не на связи — верим Telegram (следование); выходит на связь с
    `in = idle` — страховка такта возвращает эпоху."""
    svc = mv.svc
    _failed_after_release(svc)
    svc.rsv_update("rsv_peer_state", session=False)
    assert await _telegram_says(svc, HOOK) is False
    assert _following(svc), "резерв молчит, Telegram говорит «вебхук у него» — а основа решает сама"
    svc.reserve_out_tick()
    assert _following(svc), "такт без слова резерва вернул эпоху"
    rm.st_in(svc, **{"in": "idle"})
    svc.reserve_out_tick()
    assert not _following(svc), "резерв на связи без гостя — а основа всё ещё следует"


# ── 3. отказ слияния ────────────────────────────────────────────────────────

async def test_a_failed_merge_keeps_the_epoch_here_through_telegram_words_and_a_second_failure(world):
    svc = world.svc
    wrong_k = b"\x02" * 32                                  # реплика не под тем K — слияние откажет
    svc.reserve_return_start("button")
    svc.reserve_released_in(rm.snapshot_blob(svc, k=wrong_k))
    out = svc.rsv_get("rsv_out")
    assert out["state"] == "away" and out.get("guest_stopped") and out.get("merge_error"), out
    assert not _following(svc), "отказ слияния: гость остановлен, а решения «у него»"
    epoch = _epoch(svc)
    assert await _telegram_says(svc, HOOK) is False
    svc.reserve_out_tick()
    assert not _following(svc) and _epoch(svc) == epoch, "слово Telegram после отказа слияния увело эпоху"
    svc.reserve_return_start("button")
    assert not _following(svc), "повтор возврата с остановленным гостем — основа снова следует"
    svc.reserve_released_in(rm.snapshot_blob(svc, k=wrong_k))
    out = svc.rsv_get("rsv_out")
    assert out["state"] == "away" and out.get("merge_blocked"), out
    assert not _following(svc), "второй отказ слияния оставил основу в следовании"


# ── 4. сбой отправки финальной реплики ──────────────────────────────────────

async def test_a_replica_that_did_not_leave_keeps_the_epoch_here(mv, act):
    """Реплика собрана (эпоха ушла), но не ушла резерву: шаг назад в release,
    право отвечать — mine; бот на резерве не поднят — эпоха здесь."""
    svc = act.svc
    rm.to_step(svc, "release", gen=7)
    act.released_ok = False
    epoch = _epoch(svc)
    await rsvactions.run([("release",)])
    assert act.log.count("send_released") == 1, act.log
    out = svc.rsv_get("rsv_out")
    assert (out["state"], out["step"], svc.reserve_poll()) == ("leaving", "release", "mine"), out
    assert not _following(svc), "реплика не ушла — а решения у бота, которого на резерве нет"
    assert _epoch(svc) == epoch + 2, "эпоха не уходила со сборкой реплики — сценарий не о том"
    svc.reserve_out_tick()
    assert not _following(svc), "такт на шаге release увёл эпоху"
    act.released_ok = True
    await rsvactions.run([("release",)])
    assert svc.rsv_get("rsv_out")["step"] == "released" and _following(svc), "повтор передачи не отдал эпоху"


# ── 5. окно слияния ─────────────────────────────────────────────────────────

async def test_decision_jobs_do_not_run_during_the_merge_and_run_after_it(world, fake_bot, monkeypatch):
    """На шаге merge слияние пишет живую базу сырым соединением — задания решений
    по устаревшей базе в это окно не идут; на restart — идут."""
    svc = world.svc
    calls: list[int] = []
    monkeypatch.setattr(svc, "check_expiry", lambda: calls.append(1) or [])
    monkeypatch.setattr(svc, "check_pauses", lambda: [])
    sched = setup_scheduler(svc, fake_bot, svc.db)
    try:
        svc.reserve_return_start("button")
        svc.rsv_update("rsv_out", guest_stopped=True)       # реплика пришла, гость остановлен
        svc._out_step("merge", 300)
        assert _following(svc), "на слиянии основа уже держатель"
        await sched.get_job("expiry").func()
        assert calls == [], "сроки прогнаны посреди слияния по устаревшей базе"
        svc._out_step("restart", restart_at=0)
        assert not _following(svc), "слияние кончилось — эпоха не вернулась"
        await sched.get_job("expiry").func()
        assert calls == [1], "после слияния задание сроков не пошло"
    finally:
        restore_settings()


# ── 6. две отметки в одну секунду ───────────────────────────────────────────

@pytest.mark.parametrize("telegram_last, follow", [(False, False), (True, True)])
def test_home_and_a_telegram_word_in_one_second_are_ordered_by_fractions(away_offline, monkeypatch,
                                                                         telegram_last, follow):
    """Дома и «вебхук у резерва» в одну и ту же секунду ISO (резерв молчит): слово,
    сказанное до возврата, — прежнее (держатель); сказанное уже дома — верим
    Telegram (следование). Секунда одна — решает at_ts."""
    svc = away_offline
    stamp = timeutil.now_iso()
    monkeypatch.setattr(timeutil, "now_iso", lambda: stamp)
    if telegram_last:
        svc.reserve_set_out("home", step="")
        svc.reserve_set_poll("away", why="webhook_info", url=HOOK)
    else:
        svc.reserve_set_poll("away", why="webhook_info", url=HOOK)
        svc.reserve_set_out("home", step="")
    out, poll = svc.rsv_get("rsv_out"), svc.rsv_get("rsv_poll")
    assert out["since"] == poll["since"], "отметки не в одной секунде — сценарий не о том"
    assert _following(svc) is follow, \
        f"Telegram {'после' if telegram_last else 'до'} возврата в ту же секунду: следование={_following(svc)}"


@pytest.fixture()
def away_offline(mv):
    """Пользователи и бот у резерва, резерв не на связи (слово резерва роль не решает)."""
    svc = mv.svc
    rm.go_away(svc, gen=7)
    svc.rsv_update("rsv_peer_state", session=False)
    assert _following(svc)
    return svc


# ── 7. «вернуть без резерва» ────────────────────────────────────────────────

async def test_the_force_home_lever_holds_the_epoch_before_and_after_take_home(away_offline):
    """Эпоха здесь сразу по рычагу — ещё до take_home, пока право отвечать «away»
    и Telegram говорит о вебхуке; take_home её не трогает."""
    svc = away_offline
    svc.db.set_state("last_server_ok", "0")
    svc.rsv_update("rsv_peer_state", down_since=timeutil.to_iso(timeutil.shift(timeutil.now(), -11 * 60)))
    assert svc.reserve_force_check() == ""
    assert ("take_home",) in svc.reserve_force_home()
    out = svc.rsv_get("rsv_out")
    assert (out["state"], bool(out.get("await_merge")), svc.reserve_poll()) == ("away", True, "away"), out
    assert not _following(svc), "рычаг нажат — решения всё ещё у отрезанного резерва"
    epoch = _epoch(svc)
    svc.reserve_out_tick()
    assert await _telegram_says(svc, HOOK) is False         # deleteWebhook ещё не прошёл
    assert not _following(svc), "до take_home слово Telegram увело эпоху обратно"
    svc.reserve_stuck_home()                                 # take_home прошёл
    assert svc.reserve_out() == "home" and svc.rsv_get("rsv_out").get("await_merge")
    assert not _following(svc) and _epoch(svc) == epoch, "take_home сдвинул эпоху"


# ── 8. новый переезд кнопкой ────────────────────────────────────────────────

@pytest.mark.parametrize("mark", ["guest_stopped", "await_merge"])
def test_a_new_move_drops_the_marks_of_the_previous_return(mv, mark):
    """Отметка прежнего возврата, пережившая home, не должна держать эпоху здесь,
    когда новый переезд передал пользователей и бот резерву."""
    svc = mv.svc
    svc.rsv_update("rsv_out", **{mark: True})               # след прежнего возврата
    assert not _following(svc)
    svc.reserve_move_start()
    assert not svc.rsv_get("rsv_out").get(mark), f"новый переезд унаследовал {mark}"
    svc._out_step("release", 300)
    assert svc.reserve_release_build() is not None
    svc.reserve_taken_in()
    assert svc.reserve_out() == "away"
    assert _following(svc), f"гость ведёт моих пользователей, а основа решает сама ({mark} прежнего возврата)"


# ── 9. итог проверки ящика — местный ключ ───────────────────────────────────

@pytest.fixture()
def mailbox(services, monkeypatch):
    """Ящик настроен держателем, затем основа следует; IMAP/SMTP — подставные."""
    services.email_save("box@example.org", "DUMMY-password", "imap.example.org", 993, "smtp.example.org", 587)
    box = types.SimpleNamespace(fail="", sent=[])

    def check(acc):
        if box.fail:
            raise mail.MailError(box.fail)

    def send_mail(acc, to, subject, body, attachments=None):
        box.sent.append(subject)
    monkeypatch.setattr(mail, "check_imap", check)
    monkeypatch.setattr(mail, "check_smtp", check)
    monkeypatch.setattr(mail, "send_mail", send_mail)
    services.db.jrn_pass(holder="peer", role="follower", why="тест: следование")
    box.svc = services
    yield box
    restore_settings()


def test_the_follower_still_refuses_a_mailbox_decision(mailbox):
    """Контроль: запрет в силе — смена ящика у следующего получает отказ."""
    with pytest.raises(sqlite3.IntegrityError) as ei:
        mailbox.svc.db.set_state("email_login", "other@example.org")
    assert rsvjrn.is_refusal(ei.value), ei.value


def test_the_alert_fallback_mail_marks_the_mailbox_checked_while_following(mailbox):
    """Критичный алерт письмом (Telegram недоступен) — отметка «ящик рабочий» не
    роняет отправку отказом базы."""
    svc = mailbox.svc
    svc.email_send_alert("<b>сервер AWG не отвечает</b>")
    assert mailbox.sent, "письмо алерта не ушло"
    assert svc.email_last_check()[0] == "ok", "итог проверки ящика не записан у следующего"


@pytest.mark.parametrize("fail, want", [("", "ok"), ("IMAP: вход отклонён", "fail")])
def test_the_startup_mail_check_is_written_while_following(mailbox, fail, want):
    """Предстартовая проверка почты (preflight → email_check) у следующего: и
    успех, и отказ записываются, отказа базы нет."""
    mailbox.fail = fail
    ok, detail = mailbox.svc.email_check()
    assert ok is (not fail), detail
    assert mailbox.svc.email_last_check()[0] == want


# ── 10. переход эпохи не собирает пакет ─────────────────────────────────────

def test_an_epoch_pass_is_a_counters_commit(services, rsv):
    kinds: list[str] = []
    services.db.set_commit_hook(kinds.append)
    services.db.jrn_pass(holder="peer", role="follower", why="тест: следование")
    services.db.jrn_pass(holder="self", role="holder", why="тест: держатель")
    assert kinds == ["counters", "counters"], f"переход эпохи — повод для пакета переезда: {kinds}"


def test_an_epoch_pass_does_not_ask_for_a_move_package(mv):
    """Передача эпохи при уходе и возврате не ставит отметку пакета; запись данных
    пользователей — ставит (контроль крючка)."""
    svc = mv.svc
    svc.reserve_pkg_hook_install()
    svc.__dict__.pop("_rsv_pkg_due_at", None)
    epoch = _epoch(svc)
    rm.go_away(svc, gen=7)
    svc.rsv_update("rsv_peer_state", session=False)
    svc.reserve_set_out("home", step="")
    svc.reserve_set_poll("mine")
    assert _epoch(svc) == epoch + 2, "эпоха не ходила — сценарий не о том"
    assert svc.__dict__.get("_rsv_pkg_due_at") is None, "переход эпохи поставил отметку пакета переезда"
    svc.db.set_state("move_notified", "1")                   # ключ белого списка реплики — данные
    assert svc.__dict__.get("_rsv_pkg_due_at") is not None, "крючок не стоит — проверка выше ничего не доказывает"

