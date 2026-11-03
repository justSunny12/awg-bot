"""Сорванное слияние на прежнем пути возврата (без журнала): резерв возобновляет гостя
(inbound_back: _tick_released_wait, reserve_resume, reserve_resume_in), основа шлёт `resume`
(outbound_return.reserve_released_in, за флагом `ret1`).

Срыв `_return_merge` переводит основу в away, а гость на резерве уже остановлен на
`back_req` и вебхук стоит на резерве: бот нем с обеих сторон, кнопки повторить ни у кого
нет. Резерв — по живому `st` основы `away` после released (через RELEASED_WAIT_S + 60 с)
или сразу по её слову `resume` — снова поднимает юнит гостя и несёт пользователей;
`home`/`returning` основы — потерянный `back`, его повторяет сама основа. Режим Telegram —
прежний: VPN здесь без бота до возврата (tg_wait) — и после, захват повторяется.

Окружение — `host` из test_reserve_hosting (зона и systemctl — подмены), `merge_world`
из test_reserve_return (основа со снимком и начатым возвратом).

Цена ошибки: пользователи без бота с обеих сторон до ручного вмешательства; гость,
поднятый по старому `away` (st до released), — два бота на одном токене; «бот у меня»
после возобновления, которого нет, — захват Telegram больше не повторяется, бот нем.
"""
from __future__ import annotations

import pytest

from awgbot.domain.services.reserve import compat, inbound_back, inbound_tgwait
from awgbot.infra import rsvzone
from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import notices, sent
from tests.integration.test_reserve_hosting import _releasing, _serving, host
from tests.integration.test_reserve_return import away, merge_world

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["host", "away", "merge_world"]

START = ["systemctl", "start", rsvzone.GUEST_UNIT]


def _released(host) -> None:
    """Реплика гостя ушла основе (released), `back` не пришёл."""
    _releasing(host)
    host.spawn.runs.clear()
    assert sent_released(host.svc.reserve_guest_replica_in(b"\x01"))
    assert host.svc.rsv_get("rsv_in")["released_at"]


def sent_released(acts) -> bool:
    return any(a[0] == "send_released" for a in acts)


def _age(host, seconds: int) -> None:
    """released отдан `seconds` назад (отметка по часам стены)."""
    host.svc.rsv_update("rsv_in", released_at=timeutil.to_iso(timeutil.shift(timeutil.now(), -seconds)))


# ── резерв: по сроку и st основы ────────────────────────────────────────────

def test_the_reserve_restarts_the_guest_when_the_primary_is_away_after_released(host):
    svc = host.svc
    _released(host)
    rm.st_in(svc, out="away", **{"in": "idle"})
    _age(host, inbound_back.RESUME_AFTER_S - 30)
    assert svc.reserve_in_tick() == [] and svc.reserve_in() == "releasing", "гость поднят раньше срока"
    _age(host, inbound_back.RESUME_AFTER_S + 1)
    acts = svc.reserve_in_tick()
    assert host.spawn.runs == [START], host.spawn.runs
    inn = svc.rsv_get("rsv_in")
    assert svc.reserve_in() == "serving" and inn["tg_taken"] and not inn.get("released_at")
    assert ("fw",) in acts
    texts = [n.text for n in notices(acts)]
    assert len(texts) == 1 and "не состоялся" in texts[0] and "Beta" in texts[0] and not texts[0].endswith(".")
    assert "wipe" not in host.zone.names(), "хранилище гостя стёрто — возобновлять нечего"
    assert svc.reserve_in_tick() == [], "serving: повторных запусков нет"


@pytest.mark.parametrize("out", ["home", "returning"])
def test_a_primary_at_home_or_returning_means_a_lost_back_and_the_reserve_keeps_waiting(host, out):
    """Потерянный `back` повторяет основа (_home_back_resend): резерв не поднимает гостя поверх
    бота, который уже отвечает дома."""
    svc = host.svc
    _released(host)
    rm.st_in(svc, out=out, **{"in": "idle"})
    _age(host, inbound_back.RESUME_AFTER_S + 1)
    assert svc.reserve_in_tick() == [] and svc.reserve_in() == "releasing" and host.spawn.runs == []


def test_an_away_said_before_released_is_the_old_episode_and_does_not_restart_the_guest(host):
    svc = host.svc
    _releasing(host)
    rm.st_in(svc, out="away", **{"in": "idle"})               # st до released — прежний away этого же переезда
    host.spawn.runs.clear()
    svc.reserve_guest_replica_in(b"\x01")
    _age(host, inbound_back.RESUME_AFTER_S + 1)
    svc.rsv_update("rsv_peer_state", at=timeutil.to_iso(timeutil.shift(timeutil.now(), -3600)))
    assert svc.reserve_in_tick() == [] and svc.reserve_in() == "releasing" and host.spawn.runs == []


def test_a_dead_pair_link_does_not_restart_the_guest(host):
    svc = host.svc
    _released(host)
    rm.st_in(svc, out="away", **{"in": "idle"})
    _age(host, inbound_back.RESUME_AFTER_S + 1)
    svc.rsv_update("rsv_peer_state", session=False)
    assert svc.reserve_in_tick() == [] and host.spawn.runs == []


def test_a_guest_unit_that_fails_to_start_is_retried_not_more_often_than_a_minute(host):
    import subprocess
    svc = host.svc
    _released(host)
    rm.st_in(svc, out="away", **{"in": "idle"})
    _age(host, inbound_back.RESUME_AFTER_S + 1)
    host.spawn.run_error = subprocess.TimeoutExpired("systemctl", 60)
    assert svc.reserve_in_tick() == [] and svc.reserve_in() == "releasing", "юнит не поднялся, а состояние serving"
    host.spawn.run_error = None
    assert svc.reserve_in_tick() == [] and host.spawn.runs == [], "повтор запуска раньше минуты"
    host.clock.advance(inbound_back.RESUME_RETRY_S + 1)
    svc.reserve_in_tick()
    assert host.spawn.runs == [START] and svc.reserve_in() == "serving"


# ── резерв: слово `resume` ──────────────────────────────────────────────────

def test_resume_from_the_primary_restarts_the_guest_at_once(host):
    svc = host.svc
    _released(host)
    acts = svc.reserve_resume_in()
    assert host.spawn.runs == [START] and svc.reserve_in() == "serving"
    assert notices(acts) and "не состоялся" in notices(acts)[0].text
    assert svc.reserve_resume_in() == [], "повтор слова при serving — повторный запуск"


def test_resume_restores_the_vpn_without_the_bot_when_the_guest_did_not_hold_telegram(host, monkeypatch):
    """До возврата VPN был здесь без бота (tg_wait): возобновление не выдумывает «бот у меня» —
    serving без `tg_taken`, причина прежняя, захват повторяется, админу — что бота нет."""
    svc = host.svc
    _serving(host, tg_wait="гость не взял право отвечать (вебхук не поставлен)")
    svc.rsv_update("rsv_in", tg_taken=False)
    assert svc.reserve_st_body()["in_tg"] == "wait"
    svc.reserve_back_req_in()
    host.spawn.runs.clear()
    svc.reserve_guest_replica_in(b"\x01")
    acts = svc.reserve_resume_in()
    inn = svc.rsv_get("rsv_in")
    assert host.spawn.runs == [START] and svc.reserve_in() == "serving"
    assert not inn.get("tg_taken"), "после возврата — «бот у меня», которого не было"
    assert "не взял право отвечать" in inn.get("tg_wait", "") and svc.reserve_st_body()["in_tg"] == "wait"
    text = notices(acts)[0].text
    assert text == ("⚠️ Возврат пользователей «Beta» не состоялся: слияние данных у «Beta» не прошло — пользователи "
                    "«Beta» снова у тебя, VPN работает, но гостевой бот не отвечает в Telegram — повторяю раз в "
                    "минуту; возврат повторит админ «Beta»"), text
    monkeypatch.setattr(inbound_tgwait, "subprocess", host.spawn)
    monkeypatch.setattr(rsvzone, "guest_unit_active", lambda: True)
    assert not host.zone.take.exists()
    host.clock.advance(inbound_tgwait.TG_RETRY_S * 1.5)
    svc.reserve_in_tick()
    assert host.zone.take.exists(), "захват Telegram после возобновления не повторяется"


@pytest.mark.parametrize("state", ["serving", "idle", "relay"])
def test_resume_outside_a_release_changes_nothing(host, state):
    svc = host.svc
    svc.reserve_set_in(state)
    assert svc.reserve_resume_in() == [] and svc.reserve_in() == state and host.spawn.runs == []


def test_resume_before_the_replica_is_sent_is_ignored(host):
    """Гость ещё отдаёт реплику: возобновлять нечего — released ещё не было."""
    svc = host.svc
    _releasing(host)
    host.spawn.runs.clear()
    assert svc.reserve_resume_in() == [] and svc.reserve_in() == "releasing" and host.spawn.runs == []


def test_the_journal_return_is_not_resumed_this_way(host):
    svc = host.svc
    _released(host)
    svc.rsv_update("rsv_in", jrn_back={"epoch": 3, "last": 10})
    rm.st_in(svc, out="away", **{"in": "idle"})
    _age(host, inbound_back.RESUME_AFTER_S + 1)
    assert svc.reserve_in_tick() == [] and svc.reserve_resume_in() == [] and host.spawn.runs == []


# ── основа: слово `resume` за флагом ────────────────────────────────────────

def _merge_breaks(w, monkeypatch):
    def boom(blob, gen):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(w.svc, "_return_merge", boom)


def test_a_failed_merge_sends_resume_to_a_reserve_that_understands_it(merge_world, monkeypatch):
    w = merge_world
    _merge_breaks(w, monkeypatch)
    acts = w.svc.reserve_released_in(rm.snapshot_blob(w.svc))
    assert w.svc.reserve_out() == "away" and w.svc.rsv_get("rsv_out")["merge_error"]
    assert sent(acts) == ["resume"], acts
    assert any("Возврат не состоялся" in n.text for n in notices(acts))


def test_no_resume_to_a_reserve_without_the_capability(merge_world, monkeypatch):
    """Резерв прежнего выпуска слова не знает — только таймерная ветка по его стороне."""
    w = merge_world
    _merge_breaks(w, monkeypatch)
    rm.st_in(w.svc, caps=[c for c in compat.CAPS if c != "ret1"], **{"in": "serving"})
    acts = w.svc.reserve_released_in(rm.snapshot_blob(w.svc))
    assert w.svc.reserve_out() == "away" and sent(acts) == [], acts
