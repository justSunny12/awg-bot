"""Срок после done у taking на пути с K основы (inbound._tick_done_wait):
основа поменяла запись и должна отдать бота `released`. Не отдала за
DONE_BUTTON_WAIT_S и мертва для меня (peer_seen = down) — клон уже несёт
пользователей: отметка tg_take_forced, файл TG_TAKE_FILE, гостю takeover;
за TAKEN_WAIT_S после этого гость не взял Telegram — срыв. Основа жива — срыв
по сроку без takeover. Отметка не записалась — срыв, а не «взяли молча».

Мир — фикстура host из test_reserve_hosting: зона, процессы и часы подставные.

Цена ошибки: takeover при живой основе — два бота на одном токене, каждый
снимает вебхук другого; нет takeover при мёртвой — пользователи на клоне без
бота (кнопки и коды не отвечают) до ручного вмешательства; takeover без
файла-отметки — гость после перезапуска не ставит вебхук; срок без предела
после takeover — taking висит, зона занята навсегда.
"""
from __future__ import annotations

import pytest

from awgbot.domain.services.reserve import inbound
from awgbot.infra import rsvzone

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import sent
from tests.integration.test_reserve_hosting import _taking, host

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["host"]


def _done(host, peer: str, monkeypatch):
    """Переезд кнопкой (путь с K), процесс открытия кончил done; основа — peer."""
    monkeypatch.setattr(host.svc, "reserve_peer_seen", lambda *a, **k: peer)
    _taking(host, "button")
    host.zone.status = {"step": "done", "record": "skipped"}
    host.svc.reserve_in_tick()
    host.svc.reserve_in_tick()
    assert host.svc.reserve_in() == "taking"


def _down_reason(acts) -> str:
    down = [a[2]["reason"] for a in acts if a[:2] == ("send", "down")]
    return down[0] if down else ""


def test_a_dead_primary_after_done_lets_the_guest_take_telegram(host, monkeypatch):
    svc = host.svc
    _done(host, "down", monkeypatch)
    host.clock.advance(inbound.DONE_BUTTON_WAIT_S - 5)
    early = svc.reserve_in_tick()                          # повтор up основе идёт своим чередом
    assert ("guest", "takeover", {}) not in early and not svc._inn().get("tg_take_forced"), \
        "takeover раньше срока released"
    host.clock.advance(10)
    acts = svc.reserve_in_tick()
    assert ("guest", "takeover", {}) in acts, acts
    assert svc._inn().get("tg_take_forced") is True and svc.reserve_in() == "taking"
    assert rsvzone.TG_TAKE_FILE.read_text(encoding="utf-8") == "1", "гость после перезапуска не поставит вебхук"
    assert "down" not in sent(acts)
    assert ("guest", "takeover", {}) not in svc.reserve_in_tick(), "takeover повторяется на каждом такте"


def test_after_the_takeover_the_guest_has_its_own_term(host, monkeypatch):
    svc = host.svc
    _done(host, "down", monkeypatch)
    host.clock.advance(inbound.DONE_BUTTON_WAIT_S + 1)
    svc.reserve_in_tick()
    host.clock.advance(inbound.TAKEN_WAIT_S - 10)
    acts = svc.reserve_in_tick()
    assert "down" not in sent(acts) and ("guest", "takeover", {}) not in acts and svc.reserve_in() == "taking"
    host.clock.advance(15)
    acts = svc.reserve_in_tick()
    assert svc.reserve_in() == "idle" and "released" in _down_reason(acts), acts


def test_a_live_primary_that_did_not_send_released_is_a_failure_without_takeover(host, monkeypatch):
    svc = host.svc
    _done(host, "up", monkeypatch)
    host.clock.advance(inbound.DONE_BUTTON_WAIT_S + 1)
    acts = svc.reserve_in_tick()
    assert ("guest", "takeover", {}) not in acts and not svc._inn().get("tg_take_forced")
    assert svc.reserve_in() == "idle" and "released" in _down_reason(acts), acts
    assert not rsvzone.TG_TAKE_FILE.exists()


def test_a_released_replica_already_here_is_not_taken_over(host, monkeypatch):
    """Реплика пришла (replica_at), а основа потом пропала: Telegram отдаётся
    обычным путём — гость уже стартует с репликой, takeover не нужен."""
    svc = host.svc
    _done(host, "down", monkeypatch)
    svc.rsv_update("rsv_in", replica_at="2026-10-31T10:00:00+03:00")
    host.clock.advance(inbound.DONE_BUTTON_WAIT_S + 1)
    acts = svc.reserve_in_tick()
    assert ("guest", "takeover", {}) not in acts and not svc._inn().get("tg_take_forced")


def test_an_unwritable_take_mark_fails_the_hosting(host, monkeypatch):
    svc = host.svc
    _done(host, "down", monkeypatch)

    def boom(path, text, mode=0o600):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(rsvzone, "write_private", boom)
    host.clock.advance(inbound.DONE_BUTTON_WAIT_S + 1)
    acts = svc.reserve_in_tick()
    assert ("guest", "takeover", {}) not in acts
    assert svc.reserve_in() == "idle" and "tg_take" in _down_reason(acts), acts
