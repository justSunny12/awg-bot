"""Свой адрес для записи DNS (reserve/dns.py `reserve_own_ip`) и смена записи на
себя, когда он не определился (outbound `_tick_record`, `_move_fail`,
outbound_return `reserve_force_home`, срок шага record_back):

- последний успешный адрес имени — в `rsv_dns.own_last` (с именем, к которому он
  относится); резолв не удался — он, а не пусто; другое имя — не он;
- шаг записи без адреса: `record_set_at` не ставится, API не трогается, повтор
  на следующем такте; адрес появился — смена на него; дольше 2 мин — одно
  громкое Т-РУ11 «свой адрес не определился», срок шага — без второго такого;
- срыв переезда и «вернуть без резерва» при неизвестном адресе — то же
  уведомление вместо тишины.

Окружение — `mv` из _reserve_move (запись DNS, системный резолвер и часы — модели).

Цена ошибки: пустой адрес принят за «запись сменена» — 30 минут ожидания факта по
пустому адресу, пользователи на резерве, админ не знает почему; смена записи,
молча пропущенная на срыве или рычаге, — запись указывает на резерв, а
пользователи и бот уже здесь; кэш чужого имени — запись уводят на прежний адрес.
"""
from __future__ import annotations

import pytest

from awgbot.domain.notices import reserve as rn
from awgbot.domain.services.reserve import outbound
from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import OWN_IP, PEER_IP, notices, sent
from tests.integration.test_reserve_return import away

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["away"]

UNKNOWN = "не удалось определить локальный адрес"


def _ago(seconds: float) -> str:
    return timeutil.to_iso(timeutil.shift(timeutil.now(), -seconds))


def _resolver_gone(w) -> None:
    """Системный резолвер не знает своё имя, кэш памяти истёк."""
    w.dns.system = {}
    w.svc.__dict__.pop("_rsv_own_ip", None)


def _forget_last(w) -> None:
    w.svc.rsv_update("rsv_dns", own_last=None, own_last_host=None)


# ── свой адрес ──────────────────────────────────────────────────────────────

def test_the_last_resolved_own_address_is_kept_and_used_when_resolving_fails(mv):
    assert mv.svc.reserve_own_ip() == OWN_IP
    d = mv.svc.rsv_get("rsv_dns")
    assert (d.get("own_last"), d.get("own_last_host")) == (OWN_IP, rc.HOST_A), d
    _resolver_gone(mv)
    assert mv.svc.reserve_own_ip() == OWN_IP, "резолв не удался — последний успешный адрес не взят"


def test_the_kept_address_of_another_name_is_not_used(mv):
    mv.svc.rsv_update("rsv_dns", own_last="203.0.113.250", own_last_host="old.example.org")
    _resolver_gone(mv)
    assert mv.svc.reserve_own_ip() == "", "адрес прежнего имени выдан за свой"


def test_a_failed_resolve_without_a_kept_address_is_retried_not_every_tick(mv):
    _resolver_gone(mv)
    _forget_last(mv)
    reads = mv.dns.system_reads
    assert mv.svc.reserve_own_ip() == "" and mv.svc.reserve_own_ip() == ""
    assert mv.dns.system_reads == reads + 1, "неудачный резолв повторяется на каждом вызове"
    mv.dns.system = {rc.HOST_A: [OWN_IP]}
    mv.clock.advance(21)
    assert mv.svc.reserve_own_ip() == OWN_IP


# ── шаг записи без адреса ───────────────────────────────────────────────────

def _record_back(w) -> None:
    w.svc.reserve_set_out("returning", kind="button", gen=7, return_started_at=timeutil.now_iso())
    w.svc._out_step("record_back", outbound.RECORD_WAIT_S, record_set_at="", record_probe=0)


def test_the_record_step_without_an_address_does_not_count_as_switched_and_retries(away):
    rm.automatic(away.svc)
    away.dns.auth = [PEER_IP]
    _resolver_gone(away)
    _forget_last(away)
    _record_back(away)
    assert notices(away.svc.reserve_out_tick()) == []
    out = away.svc.rsv_get("rsv_out")
    assert not out.get("record_set_at") and away.dns.sets == [], f"пустой адрес принят за смену записи: {out}"
    away.dns.system = {rc.HOST_A: [OWN_IP]}
    away.clock.advance(21)
    away.svc.reserve_out_tick()
    assert away.dns.sets == [OWN_IP] and away.svc.rsv_get("rsv_out").get("record_set_at"), away.dns.sets


def test_the_record_step_without_an_address_tells_once_after_two_minutes(away):
    rm.automatic(away.svc)
    away.dns.auth = [PEER_IP]
    _resolver_gone(away)
    _forget_last(away)
    _record_back(away)
    away.svc.reserve_out_tick()
    away.svc.rsv_update("rsv_out", record_noip_at=_ago(outbound.RECORD_NOIP_TELL_S + 1))
    told = notices(away.svc.reserve_out_tick())
    assert len(told) == 1 and told[0].force_sound and UNKNOWN in told[0].text and rc.HOST_A in told[0].text, \
        [n.text for n in told]
    assert notices(away.svc.reserve_out_tick()) == [], "одно и то же уведомление на каждом такте"
    away.clock.advance(outbound.RECORD_WAIT_S + 1)
    acts = away.svc.reserve_out_tick()
    assert sent(acts) == ["back"] and notices(acts) == [], "срок шага повторил уже сказанное"


def test_the_record_step_switches_to_the_kept_address(away):
    rm.automatic(away.svc)
    away.dns.auth = [PEER_IP]
    assert away.svc.reserve_own_ip() == OWN_IP                     # однажды разрешилось
    _resolver_gone(away)
    _record_back(away)
    away.svc.reserve_out_tick()
    assert away.dns.sets == [OWN_IP], "смена записи не на последний успешный адрес"


# ── срыв переезда и рычаг ───────────────────────────────────────────────────

def test_a_failed_move_with_the_record_on_the_reserve_and_no_own_address_tells(mv):
    rm.automatic(mv.svc)
    rm.to_step(mv.svc, "release")
    mv.svc.rsv_update("rsv_out", record_set_at=timeutil.now_iso())
    _resolver_gone(mv)
    _forget_last(mv)
    acts = mv.svc._move_fail("передача бота не завершилась", released=True)
    texts = [n.text for n in notices(acts)]
    assert mv.dns.sets == [] and any(UNKNOWN in t for t in texts), texts


def test_the_lever_with_no_own_address_tells_instead_of_silence(away):
    rm.automatic(away.svc)
    away.svc.db.set_state("last_server_ok", "0")
    away.svc.rsv_update("rsv_peer_state", session=False, down_since=_ago(11 * 60))
    _resolver_gone(away)
    _forget_last(away)
    acts = away.svc.reserve_force_home()
    told = notices(acts)
    assert ("take_home",) in acts and away.dns.sets == []
    assert len(told) == 1 and UNKNOWN in told[0].text and told[0].force_sound, [n.text for n in told]


def test_the_notice_text():
    text = rn.record_own_unknown("vpn.example.org", "vpn.example.org")
    assert text.startswith("⚠️ Запись <code>vpn.example.org</code> не изменена: не удалось определить локальный адрес "
                           "(домен <code>vpn.example.org</code> не резолвится) — поправь запись вручную у DNS-провайдера"), text
    assert not text.endswith(".")
    assert "(" not in rn.record_own_unknown("vpn.example.org")
