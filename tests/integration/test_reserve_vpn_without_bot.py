"""VPN основы у резерва, а бот — нет; бот у гостя по принудительному захвату
(inbound_tgwait, inbound `_tick_done_wait`, inbound_back `reserve_back_in`,
outbound_forced, `st.in_tg`).

Сторона резерва, аварийный путь (открытие по точкам или коду): процесс открытия
сам сменил запись DNS (`done` без `record=skipped`), а гость Telegram не взял за
TAKEN_WAIT_S — зона остаётся (serving без `tg_taken`, причина — в карточке),
админу резерва — уведомление, захват повторяется раз в минуту с разбросом
(отметка `tg_take`, юнит гостя снова, если не активен); гость взял — Т-РУ4 и
`taken` обычным путём; `back` основы зону снимает. Путь с K основы: гость взял
Telegram по принудительному захвату (основа не отдала `released` и пропала) —
`back` ожившей основы отвечается `taken`, гость и его правки остаются.

Сторона основы: срок шага переезда (up, record, release), а живой `st`
резерва — `in = serving`, `in_tg = forced`: away без `back` и без возврата записи;
`st` резерва прежний (перезапуск основы) — подождать свежий до FORCED_ST_WAIT_S,
потом срыв как прежде; живой резерв без захвата — срыв как прежде.

Мир — фикстуры host (test_reserve_hosting) и mv (_reserve_move): зона, процессы,
часы и DNS подставные.

Цена ошибки: зона, снятая из-за Telegram после смены записи, — пользователи идут на
адрес резерва, где клона уже нет, и сидят без VPN, хотя он работал; повтор захвата
без разброса — строго периодический маячок; `back`, снёсший гостя-держателя, — все
правки админа на резерве за время аварии потеряны; срыв переезда у ожившей основы
вместо away — запись возвращается на основу, пока бот у гостя, два бота на одном
токене; решение по старому `st` после перезапуска — тот же срыв в первую секунду.
"""
from __future__ import annotations

import pytest

from awgbot.bot.texts.reserve.section import card_text
from awgbot.domain.services.reserve import inbound, inbound_tgwait, outbound, outbound_forced
from awgbot.infra import rsvzone
from awgbot.util import backoff, pairproto, timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import notices, sent
from tests.integration.test_reserve_hosting import _taking, host

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["host"]


# ── резерв: VPN без бота после смены записи процессом открытия ──────────────

def _emergency_done(host, monkeypatch, *, active: bool = False, record: str = "") -> None:
    """Открытие по точкам дошло до done (запись сменил процесс, если record не skipped)."""
    monkeypatch.setattr(inbound_tgwait, "subprocess", host.spawn)
    monkeypatch.setattr(rsvzone, "guest_unit_active", lambda: host.__dict__.get("guest_active", active))
    host.guest_active = active
    _taking(host, "dead")
    host.svc.rsv_update("rsv_in", by="share")
    host.zone.status = {"step": "done", **({"record": record} if record else {})}
    host.svc.reserve_in_tick()
    host.svc.reserve_in_tick()


def _past_taken_wait(host) -> list[tuple]:
    host.clock.advance(inbound.TAKEN_WAIT_S + 1)
    return host.svc.reserve_in_tick()


@pytest.mark.parametrize("active, why", [(False, "не поднялся"), (True, "не взял право отвечать")])
def test_a_bot_failure_after_the_record_moved_keeps_the_zone(host, monkeypatch, active, why):
    svc = host.svc
    _emergency_done(host, monkeypatch, active=active)
    acts = _past_taken_wait(host)
    inn = svc.rsv_get("rsv_in")
    assert svc.reserve_in() == "serving" and not inn.get("tg_taken"), inn
    assert why in inn.get("tg_wait", ""), inn
    assert not {"teardown", "wipe"} & set(host.zone.names()), f"рабочий клон снят из-за Telegram: {host.zone.calls}"
    assert "down" not in sent(acts), "основе — «не принял», а пользователи уже здесь"
    text = notices(acts)[0].text
    assert "VPN работает" in text and why in text and "повторяю раз в минуту" in text, text
    assert svc.reserve_st_body()["in_tg"] == "wait"
    card = card_text(svc.reserve_card_data())
    assert "└ ⚠️ гостевой бот не отвечает в Telegram" in card and why in card, card


def test_without_the_record_change_a_bot_failure_still_ends_the_hosting(host, monkeypatch):
    svc = host.svc
    _emergency_done(host, monkeypatch, record="skipped")
    acts = _past_taken_wait(host)
    assert svc.reserve_in() == "idle" and "down" in sent(acts) and "teardown" in host.zone.names()


def test_the_takeover_is_retried_about_once_a_minute_and_restarts_an_inactive_guest(host, monkeypatch):
    svc = host.svc
    monkeypatch.setattr(backoff.random, "uniform", lambda a, b: 1.0)
    _emergency_done(host, monkeypatch, active=False)
    _past_taken_wait(host)
    host.zone.take.unlink(missing_ok=True)                    # отметку стёрли — повтор вернёт
    host.spawn.runs.clear()
    host.clock.advance(inbound_tgwait.TG_RETRY_S - 5)
    assert svc.reserve_in_tick() == [] and not host.spawn.runs, "повтор раньше срока"
    host.clock.advance(10)
    svc.reserve_in_tick()
    assert ["systemctl", "start", rsvzone.GUEST_UNIT] in host.spawn.runs, host.spawn.runs
    assert host.zone.take.read_text(encoding="utf-8") == "1", "без отметки гость вебхук не поставит"
    host.spawn.runs.clear()
    host.guest_active = True
    host.clock.advance(inbound_tgwait.TG_RETRY_S + 1)
    svc.reserve_in_tick()
    assert not host.spawn.runs, "живой гость перезапущен — сам ставит вебхук, пока отметка есть"


def test_the_retry_interval_is_jittered(host, monkeypatch):
    seen = []
    monkeypatch.setattr(backoff.random, "uniform", lambda a, b: seen.append((a, b)) or 1.4)
    _emergency_done(host, monkeypatch, active=True)
    _past_taken_wait(host)
    first = host.svc.rsv_get("rsv_in").get("tg_retry_mono")
    host.clock.advance(inbound_tgwait.TG_RETRY_S * 1.3)
    host.svc.reserve_in_tick()
    assert seen and seen[-1] == (0.6, 1.4), "строго периодический повтор"
    assert host.svc.rsv_get("rsv_in").get("tg_retry_mono") == first, "повтор раньше разброса"
    host.clock.advance(inbound_tgwait.TG_RETRY_S * 0.2)
    host.svc.reserve_in_tick()
    assert host.svc.rsv_get("rsv_in").get("tg_retry_mono") != first, "повтора нет и после разброса"


def test_a_late_takeover_by_the_guest_ends_the_wait_the_usual_way(host, monkeypatch):
    svc = host.svc
    _emergency_done(host, monkeypatch)
    _past_taken_wait(host)
    acts = svc.reserve_guest_tg_taken()
    inn = svc.rsv_get("rsv_in")
    assert svc.reserve_in() == "serving" and inn.get("tg_taken") and not inn.get("tg_wait"), inn
    assert "taken" in sent(acts) and "у тебя" in notices(acts)[0].text
    assert "in_tg" not in svc.reserve_st_body()
    assert "не отвечает в Telegram" not in card_text(svc.reserve_card_data())


def test_back_of_the_primary_takes_the_waiting_zone_down(host, monkeypatch):
    svc = host.svc
    _emergency_done(host, monkeypatch)
    _past_taken_wait(host)
    acts = svc.reserve_back_in()
    assert svc.reserve_in() == "relay" and "wipe" in host.zone.names() and sent(acts)[-1] == "wiped"


def test_the_zone_removed_past_the_bot_ends_the_wait(host, monkeypatch):
    svc = host.svc
    _emergency_done(host, monkeypatch)
    _past_taken_wait(host)
    host.zone.phase = "idle"                                  # awg-bot reserve teardown
    svc.reserve_in_tick()
    assert svc.reserve_in() == "idle"


def test_the_st_field_passes_the_pair_protocol_and_is_kept_by_the_primary(mv):
    body = rm.peer_st(**{"in": "serving", "in_tg": "forced"})
    assert pairproto.check(pairproto.PAIR_PROTO, "st", dict(body), pairproto.ACCEPTOR)["in_tg"] == "forced"
    rm.st_in(mv.svc, **{"in": "serving", "in_tg": "forced"})
    assert mv.svc.reserve_peer_state()["in_tg"] == "forced"
    rm.st_in(mv.svc)
    assert mv.svc.reserve_peer_state()["in_tg"] == "", "прежнее слово пережило st без поля"


# ── резерв: бот у гостя принудительным захватом, `back` ожившей основы ──────

def _forced_holder(host, monkeypatch) -> None:
    """Переезд кнопкой: основа пропала между up и released, гость забрал Telegram сам и взял его."""
    monkeypatch.setattr(host.svc, "reserve_peer_seen", lambda *a, **k: "down")
    _taking(host, "button")
    host.zone.status = {"step": "done", "record": "skipped"}
    host.svc.reserve_in_tick()
    host.svc.reserve_in_tick()
    host.clock.advance(inbound.DONE_BUTTON_WAIT_S + 1)
    assert ("guest", "takeover", {}) in host.svc.reserve_in_tick()


def test_back_of_a_revived_primary_does_not_wipe_a_guest_holding_telegram_by_force(host, monkeypatch):
    svc = host.svc
    _forced_holder(host, monkeypatch)
    svc.reserve_guest_tg_taken()
    assert svc.reserve_in() == "serving" and svc.reserve_st_body()["in_tg"] == "forced"
    host.zone.calls.clear()
    acts = svc.reserve_back_in()
    assert sent(acts) == ["taken"], acts
    assert host.zone.calls == [] and svc.reserve_in() == "serving", "гость-держатель снят — правки за аварию потеряны"


def test_back_before_the_guest_took_telegram_takes_the_zone_down_as_before(host, monkeypatch):
    """Захват объявлен, а гость Telegram ещё не взял — основа жива и сказала back: бот у неё."""
    svc = host.svc
    _forced_holder(host, monkeypatch)
    svc.reserve_back_in()
    assert "wipe" in host.zone.names() and svc.reserve_in() == "relay"


def test_a_new_opening_does_not_inherit_the_forced_mark(host, monkeypatch):
    svc = host.svc
    svc.rsv_update("rsv_in", tg_take_forced=True, tg_wait="x")
    assert svc.reserve_go_req_in(7, "button") == ""
    inn = svc.rsv_get("rsv_in")
    assert not inn.get("tg_take_forced") and not inn.get("tg_wait"), \
        "отметка прошлого эпизода — гостю takeover на первом hello нового"


# ── основа: срок шага переезда, а бот у гостя принудительно ─────────────────

def _leaving(mv, monkeypatch, step: str, wait: int) -> None:
    svc = mv.svc
    rm.automatic(svc)
    rm.to_step(svc, step, wait=wait)
    svc.rsv_update("rsv_out", go_sent=True, record_set_at=timeutil.now_iso() if step != "up" else "")
    monkeypatch.setattr(svc, "reserve_peer_seen", lambda *a, **k: "ok")


@pytest.mark.parametrize("step, wait", [("up", outbound.UP_WAIT_S), ("record", outbound.RECORD_WAIT_S),
                                        ("release", outbound.RELEASED_WAIT_S)])
def test_a_revived_primary_seeing_the_forced_takeover_goes_away_instead_of_failing(mv, monkeypatch, step, wait):
    svc = mv.svc
    _leaving(mv, monkeypatch, step, wait)
    rm.st_in(svc, **{"in": "serving", "in_tg": "forced"})
    mv.clock.advance(wait + 1)
    acts = svc.reserve_out_tick()
    assert svc.reserve_out() == "away" and svc.reserve_poll() == "away", svc.rsv_get("rsv_out")
    assert "back" not in sent(acts), "back снёс бы гостя-держателя"
    assert mv.dns.sets == [], "запись вернулась на основу, пока бот у гостя"
    assert "на «Beta»" in notices(acts)[0].text


def test_a_stale_st_after_a_restart_is_waited_for_then_the_move_fails_as_before(mv, monkeypatch):
    svc = mv.svc
    _leaving(mv, monkeypatch, "record", outbound.RECORD_WAIT_S)
    rm.st_in(svc, **{"in": "taking"})
    svc.rsv_update("rsv_peer_state", st_stale=True)           # перезапуск основы: st резерва — прошлый
    mv.clock.advance(outbound.RECORD_WAIT_S + 1)
    assert svc.reserve_out_tick() == [] and svc.reserve_out() == "leaving", "срыв по старому взгляду"
    rm.st_in(svc, **{"in": "serving", "in_tg": "forced"})     # свежий st дошёл
    svc.reserve_out_tick()
    assert svc.reserve_out() == "away"


def test_without_a_fresh_st_the_wait_is_bounded(mv, monkeypatch):
    svc = mv.svc
    _leaving(mv, monkeypatch, "record", outbound.RECORD_WAIT_S)
    svc.rsv_update("rsv_peer_state", st_stale=True)
    mv.clock.advance(outbound.RECORD_WAIT_S + 1)
    assert svc.reserve_out_tick() == []
    mv.clock.advance(outbound_forced.FORCED_ST_WAIT_S + 1)
    acts = svc.reserve_out_tick()
    assert svc.reserve_out() == "home" and "back" in sent(acts), acts


def test_a_live_reserve_without_the_takeover_fails_the_move_as_before(mv, monkeypatch):
    svc = mv.svc
    _leaving(mv, monkeypatch, "record", outbound.RECORD_WAIT_S)
    rm.st_in(svc, **{"in": "taking"})
    mv.clock.advance(outbound.RECORD_WAIT_S + 1)
    acts = svc.reserve_out_tick()
    assert svc.reserve_out() == "home" and "back" in sent(acts)


def test_before_the_key_was_given_there_is_no_wait(mv, monkeypatch):
    """K резерву не ушёл (go_sent нет) — открыть пакет ему нечем: срыв сразу, без ожидания st."""
    svc = mv.svc
    rm.to_step(svc, "up", wait=outbound.UP_WAIT_S)
    svc.rsv_update("rsv_peer_state", st_stale=True)
    mv.clock.advance(outbound.UP_WAIT_S + 1)
    svc.reserve_out_tick()
    assert svc.reserve_out() == "home"
