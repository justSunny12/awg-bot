"""Исход смены записи DNS процессом открытия неизвестен — сторона бота резерва
(inbound_recwait, inbound `_take_fail`, inbound_keep `_rec_observe`).

Аварийный путь (по точкам кворума или по коду): процесс открытия пишет шаг-намерение
`record_try{name, ip, limit}` до запроса к провайдеру. Процесс погиб между запросом и шагом
`record_set` (OOM, перезагрузка юнита) или отказал посреди неизвестного (`fail{record=try}`) —
это не «запись не сменена»: бот читает авторитетный ответ сразу; адрес мой — как после
`record_fact` (отметка `tg_take`, такт идёт как по `done`); нет — ожидание факта раз в минуту ±
40 % до предела от намерения, зона и клоны стоят; не дождался — прежний срыв со снятием зоны.
Процесс жив и ждёт сам (`record_wait`) — бот только говорит админу: одно уведомление на вход в
ожидание, одно на исход.

Мир — фикстуры host (test_reserve_hosting) и mv (_reserve_move): зона, процессы, часы и DNS
подставные.

Цена ошибки: зона, снятая при применённой правке, — запись на адресе резерва, клона нет,
основа мертва: VPN ни у кого (по коду — до нового ввода кода админом); ожидание без предела —
неприменённая правка держит зону вечно, кворум не откроет заново; ожидание по строгому
расписанию — маячок; два уведомления на одно событие — шум в аварию.
"""
from __future__ import annotations

import pytest

from awgbot.domain.services.reserve import inbound, inbound_keep, inbound_recwait
from awgbot.util import backoff

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import notices, sent
from tests.integration.test_reserve_hosting import host

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["host"]

NAME, IP = "vpn.example.net", "203.0.113.20"
POINTS = {"qpoints": [[1, "ab" * 16], [2, "cd" * 16]], "qbox": "00" * 56}
CODE = {"code": "DUMMY-code", "cpoint": [1, "ab" * 16], "cbox": "00" * 56}
TRY = {"step": "record_try", "name": NAME, "ip": IP, "limit": "300"}


@pytest.fixture(autouse=True)
def _steady(monkeypatch):
    monkeypatch.setattr(backoff.random, "uniform", lambda a, b: 1.0)   # разброс — отдельным тестом


def _open(host, monkeypatch, by: str = "points") -> None:
    """Аварийное открытие (по точкам кворума или по коду) дошло до намерения смены записи."""
    svc = host.svc
    monkeypatch.setattr(svc, "reserve_auto_block", lambda: "")
    monkeypatch.setattr(svc, "_code_block", lambda: "")
    monkeypatch.setattr(svc, "reserve_zone_shell_ensure", lambda: None)
    secret, kind = (dict(POINTS), "dead") if by == "points" else (dict(CODE), "code")
    svc.reserve_open_by_points(secret, kind, "q")
    assert svc.reserve_in() == "taking" and len(host.spawn.popen) == 1, "подготовка: открытие не пошло"
    svc.rsv_update("rsv_in", deadline=host.clock.t + inbound.OPEN_WAIT_S)   # срок — по часам теста
    host.zone.status = {"step": "clone_up"}
    svc.reserve_in_tick()
    host.zone.status = dict(TRY)
    assert svc.reserve_in_tick() == [], "намерение само по себе — не повод говорить админу"
    inn = svc.rsv_get("rsv_in")
    assert inn.get("rec_try") and not inn.get("rec_moved") and inn.get("rec_limit") == 300, inn


def _killed(host) -> list[tuple]:
    """Процесс погиб, не написав ни record_set, ни итога: open.status — намерение."""
    host.spawn.rc = 137
    return host.svc.reserve_in_tick()


def _no_teardown(host) -> None:
    assert not {"teardown", "wipe"} & set(host.zone.names()), f"зона снята при неизвестном исходе: {host.zone.calls}"


def _texts(acts) -> list[str]:
    return [n.text for n in notices(acts)]


# ── процесс погиб между запросом и record_set ───────────────────────────────

@pytest.mark.parametrize("by", ["points", "code"])
def test_a_process_killed_after_an_applied_record_change_keeps_the_zone_and_finishes(host, monkeypatch, by):
    svc = host.svc
    _open(host, monkeypatch, by)
    host.dns.auth = [IP]                                      # правка у провайдера легла до гибели процесса
    reads = host.dns.auth_reads
    acts = _killed(host)
    inn = svc.rsv_get("rsv_in")
    _no_teardown(host)
    assert host.dns.auth_reads == reads + 1, "проверка авторитетного ответа не первым шагом"
    assert svc.reserve_in() == "taking" and inn.get("rec_moved") and inn.get("rec_done") and not inn.get("rec_wait"), inn
    assert host.zone.take.read_text(encoding="utf-8") == "1", "факт есть — гость должен ставить вебхук"
    assert "down" not in sent(acts) and not notices(acts), "ожидания не было — ни срыва, ни уведомлений"
    svc.reserve_in_tick()
    assert host.zone.names() == ["serve"], f"после факта такт не пошёл как по done: {host.zone.calls}"
    acts = svc.reserve_guest_tg_taken()
    assert svc.reserve_in() == "serving" and svc.rsv_get("rsv_in").get("tg_taken") and "taken" in sent(acts)
    _no_teardown(host)


def test_a_process_killed_before_the_record_shows_waits_once_a_minute_then_finishes(host, monkeypatch):
    svc = host.svc
    _open(host, monkeypatch)
    host.dns.auth = [rm.OWN_IP]                               # авторитетный сервер ещё отдаёт прежний адрес
    acts = _killed(host)
    inn = svc.rsv_get("rsv_in")
    assert svc.reserve_in() == "taking" and inn.get("rec_wait") and not inn.get("rec_moved"), inn
    _no_teardown(host)
    assert "down" not in sent(acts) and not host.zone.take.exists()
    texts = _texts(acts)
    assert len(texts) == 1 and texts[0].startswith("⏳ Запись <code>vpn.example.net</code>"), texts
    assert "успешность смены на <code>203.0.113.20</code> не подтверждена" in texts[0] and "до 5 мин" in texts[0], texts
    assert "код 137" in texts[0], "причина входа в ожидание не сказана"
    reads = host.dns.auth_reads
    host.clock.advance(inbound_keep.KEEP_RETRY_S - 5)
    assert svc.reserve_in_tick() == [] and host.dns.auth_reads == reads, "чтение раньше минуты"
    host.clock.advance(10)
    assert svc.reserve_in_tick() == [] and host.dns.auth_reads == reads + 1, "второе уведомление о том же ожидании"
    host.dns.auth = [IP]
    host.clock.advance(inbound_keep.KEEP_RETRY_S + 1)
    acts = svc.reserve_in_tick()
    texts = _texts(acts)
    assert len(texts) == 1 and texts[0].startswith("✅ Авторитетный DNS-сервер подтвердил адрес <code>203.0.113.20</code> для <code>vpn.example.net</code>"), texts
    assert host.zone.take.exists() and svc.rsv_get("rsv_in").get("rec_done")
    svc.reserve_in_tick()
    assert "serve" in host.zone.names() and svc.reserve_in() == "taking"
    _no_teardown(host)


@pytest.mark.parametrize("by", ["points", "code"])
def test_a_record_that_never_shows_fails_as_before_at_the_limit(host, monkeypatch, by):
    svc = host.svc
    _open(host, monkeypatch, by)
    host.dns.auth = [rm.OWN_IP]
    _killed(host)
    host.clock.advance(240)
    svc.reserve_in_tick()
    assert svc.reserve_in() == "taking", "срыв раньше предела"
    _no_teardown(host)
    host.clock.advance(61)                                    # предел — 300 с от намерения
    reads = host.dns.auth_reads
    acts = svc.reserve_in_tick()
    assert host.dns.auth_reads == reads + 1, "на пределе — последнее чтение, не срыв вслепую"
    assert svc.reserve_in() == "idle" and {"teardown", "wipe"} <= set(host.zone.names()), host.zone.calls
    down = [a[2] for a in acts if a[:2] == ("send", "down")]
    assert down and down[0]["reason"].startswith("обновление DNS-записи не подтвердилось за 5 мин"), acts
    assert not down[0].get("rec"), "запись не сменена — основе возвращать нечего"
    texts = _texts(acts)
    assert any("принять не удалось: обновление DNS-записи не подтвердилось за 5 мин" in t for t in texts), texts
    inn = svc.rsv_get("rsv_in")
    assert not any(inn.get(k) for k in ("rec_try", "rec_wait", "rec_done", "rec_told")), inn


def test_the_limit_comes_from_the_intent_manual_is_30_minutes(host, monkeypatch):
    svc = host.svc
    _open(host, monkeypatch)
    host.zone.status = {**TRY, "limit": "1800"}               # «вручную»: запись меняет человек
    svc.rsv_update("rsv_in", rec_try=False)                   # намерение увидено впервые с этим пределом
    svc.reserve_in_tick()
    host.dns.auth = [rm.OWN_IP]
    acts = _killed(host)
    assert "до 30 мин" in _texts(acts)[0]
    host.clock.advance(1700)
    svc.reserve_in_tick()
    assert svc.reserve_in() == "taking"
    host.clock.advance(101)
    acts = svc.reserve_in_tick()
    assert svc.reserve_in() == "idle" and any("за 30 мин" in t for t in _texts(acts))


def test_the_poll_is_jittered(host, monkeypatch):
    _open(host, monkeypatch)
    host.dns.auth = [rm.OWN_IP]
    _killed(host)
    seen = []
    monkeypatch.setattr(backoff.random, "uniform", lambda a, b: seen.append((a, b)) or 1.4)
    reads = host.dns.auth_reads
    host.clock.advance(inbound_keep.KEEP_RETRY_S * 1.3)
    host.svc.reserve_in_tick()
    assert seen and seen[-1] == (0.6, 1.4) and host.dns.auth_reads == reads, "чтение по строгому расписанию"
    host.clock.advance(inbound_keep.KEEP_RETRY_S * 0.2)
    host.svc.reserve_in_tick()
    assert host.dns.auth_reads == reads + 1


# ── процесс жив и ждёт сам ──────────────────────────────────────────────────

def test_a_waiting_process_is_told_once_and_its_confirmation_once(host, monkeypatch):
    svc = host.svc
    _open(host, monkeypatch)
    host.zone.status = {"step": "record_wait", "name": NAME, "ip": IP, "limit": "300",
                        "why": "провайдер DNS не ответил: timed out"}
    reads = host.dns.auth_reads
    texts = _texts(svc.reserve_in_tick())
    assert len(texts) == 1 and "провайдер DNS не ответил: timed out" in texts[0] and "до 5 мин" in texts[0], texts
    assert host.dns.auth_reads == reads, "процесс жив и ждёт сам — бот авторитетный сервер не дёргает"
    assert _texts(svc.reserve_in_tick()) == [], "вход в ожидание сказан дважды"
    host.zone.status = {"step": "record_fact", "name": NAME, "ip": IP}
    texts = _texts(svc.reserve_in_tick())
    assert len(texts) == 1 and "подтвердил" in texts[0], texts
    assert svc.rsv_get("rsv_in").get("rec_moved")
    host.zone.status = {"step": "done"}
    assert _texts(svc.reserve_in_tick()) == [] and "serve" in host.zone.names()
    _no_teardown(host)


def test_a_waiting_process_that_gives_up_ends_the_hosting_as_before(host, monkeypatch):
    """Итог процесса после своего ожидания — без поля record: исход известен, бот не ждёт второй раз."""
    svc = host.svc
    _open(host, monkeypatch)
    host.zone.status = {"step": "record_wait", "name": NAME, "ip": IP, "limit": "300", "why": "timed out"}
    svc.reserve_in_tick()
    host.zone.status = {"step": "fail", "reason": "обновление DNS-записи не подтвердилось за 5 мин: timed out", "pre_m": ""}
    acts = svc.reserve_in_tick()
    assert svc.reserve_in() == "idle" and {"teardown", "wipe"} <= set(host.zone.names())
    assert any("за 5 мин" in t for t in _texts(acts))


def test_a_failure_carrying_record_try_is_taken_over(host, monkeypatch):
    svc = host.svc
    _open(host, monkeypatch)
    host.dns.auth = [rm.OWN_IP]
    host.zone.status = {"step": "fail", "reason": "OSError: сокет не создан", "record": "try", "name": NAME, "ip": IP,
                        "limit": "300", "pre_m": ""}
    acts = svc.reserve_in_tick()
    assert svc.reserve_in() == "taking" and svc.rsv_get("rsv_in").get("rec_wait") and "down" not in sent(acts)
    assert "OSError" in _texts(acts)[0]
    _no_teardown(host)


def test_a_live_process_silent_past_the_limit_is_taken_over(host, monkeypatch):
    """Процесс жив, а итога нет дольше предела и запаса: ожидание веду я (предел вышел — последнее чтение и срыв)."""
    svc = host.svc
    _open(host, monkeypatch)
    host.dns.auth = [rm.OWN_IP]
    host.clock.advance(300 + inbound_recwait.REC_GRACE_S - 5)
    svc.reserve_in_tick()
    assert svc.reserve_in() == "taking" and not svc.rsv_get("rsv_in").get("rec_wait"), "перехват раньше срока"
    host.clock.advance(10)
    reads = host.dns.auth_reads
    acts = svc.reserve_in_tick()
    assert host.dns.auth_reads == reads + 1 and svc.reserve_in() == "idle", svc.rsv_get("rsv_in")
    assert any("за 5 мин" in t for t in _texts(acts))


def test_a_live_process_silent_past_the_limit_with_the_record_applied_finishes(host, monkeypatch):
    svc = host.svc
    _open(host, monkeypatch)
    host.dns.auth = [IP]
    host.clock.advance(300 + inbound_recwait.REC_GRACE_S + 1)
    svc.reserve_in_tick()
    assert svc.rsv_get("rsv_in").get("rec_done") and host.zone.take.exists()
    _no_teardown(host)


# ── границы правила ─────────────────────────────────────────────────────────

def test_a_failure_before_the_intent_ends_the_hosting_as_before(host, monkeypatch):
    svc = host.svc
    monkeypatch.setattr(svc, "reserve_auto_block", lambda: "")
    monkeypatch.setattr(svc, "reserve_zone_shell_ensure", lambda: None)
    svc.reserve_open_by_points(dict(POINTS), "dead", "q")
    svc.rsv_update("rsv_in", deadline=host.clock.t + inbound.OPEN_WAIT_S)
    host.zone.status = {"step": "clone_up"}
    svc.reserve_in_tick()
    acts = _killed(host)
    assert svc.reserve_in() == "idle" and "down" in sent(acts), "намерения не было — срыв прежний"


def test_after_the_record_is_mine_a_relaunched_intent_is_not_unknown(host, monkeypatch):
    """Запись уже моя: намерение перезапущенного процесса исход не делает неизвестным, итог прошлого — не его."""
    svc = host.svc
    _open(host, monkeypatch)
    host.dns.auth = [IP]
    _killed(host)
    svc.rsv_update("rsv_in", relaunch=True, keep_mono=0)      # клона нет — перезапуск (inbound_keep)
    host.spawn.rc = None
    svc.reserve_in_tick()
    inn = svc.rsv_get("rsv_in")
    assert len(host.spawn.popen) == 2 and not inn.get("rec_done"), inn
    host.zone.status = dict(TRY)
    svc.reserve_in_tick()
    assert not svc.rsv_get("rsv_in").get("rec_try") and svc.reserve_in() == "taking"


def test_a_new_opening_does_not_inherit_the_unknown(host):
    svc = host.svc
    svc.rsv_update("rsv_in", rec_try=True, rec_wait=True, rec_done=True, rec_told="wait", rec_until=5)
    assert svc.reserve_go_req_in(7, "button") == ""
    inn = svc.rsv_get("rsv_in")
    assert not any(inn.get(k) for k in ("rec_try", "rec_wait", "rec_done", "rec_told", "rec_until")), inn
