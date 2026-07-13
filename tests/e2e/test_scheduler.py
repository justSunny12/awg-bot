"""E2E: обёртки фоновых задач scheduler.py (замыкания job_* + _service_failure_alerts).

Замыкания достаём из собранного планировщика через get_job(id).func и зовём
напрямую — без реального AsyncIOScheduler.start(). Проверяем guard-логику
месячного сброса/бэкапа (catch-up + защита от двойного), проглатывание ошибок
опросчиком и гистерезис громкого алерта простоя сервиса.
"""
import datetime

import pytest

from awgbot.runtime.scheduler import setup_scheduler, _service_failure_alerts
from awgbot.core import config
from awgbot.infra import awg
from awgbot.util import timeutil

pytestmark = pytest.mark.e2e


def _jobs(services, bot):
    sched = setup_scheduler(services, bot, services.db)
    return {jid: sched.get_job(jid).func
            for jid in ("poll", "expiry", "monthly", "backup", "monitor")}


# ── job_monthly: guard + catch-up ────────────────────────────────────────────
async def test_job_monthly_first_run_only_records(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=9000)
    dc = services.add_device(client.id, "d")
    services.db.add_traffic(dc.device_id, 100, 100)
    ym = timeutil.now().strftime("%Y-%m")
    await _jobs(services, fake_bot)["monthly"]()             # state пуст → только фиксация
    assert services.db.get_state("last_monthly_reset") == ym
    dev = services.db.get_device(dc.device_id)
    assert dev.traffic_rx_month == 100                       # сброса НЕ было


async def test_job_monthly_same_month_is_noop(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=9001)
    dc = services.add_device(client.id, "d")
    ym = timeutil.now().strftime("%Y-%m")
    services.db.set_state("last_monthly_reset", ym)          # уже сбрасывали в этом месяце
    services.db.add_traffic(dc.device_id, 50, 50)
    await _jobs(services, fake_bot)["monthly"]()
    assert services.db.get_device(dc.device_id).traffic_rx_month == 50   # не тронуто


async def test_job_monthly_catch_up_resets_after_downtime(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=9002)
    dc = services.add_device(client.id, "d")
    services.db.set_state("last_monthly_reset", "2020-01")   # «проспали» границу месяца
    services.db.add_traffic(dc.device_id, 70, 30)
    await _jobs(services, fake_bot)["monthly"]()
    dev = services.db.get_device(dc.device_id)
    assert dev.traffic_rx_month == 0 and dev.traffic_tx_month == 0   # навёрстан сброс
    assert services.db.get_state("last_monthly_reset") == timeutil.now().strftime("%Y-%m")


# ── job_backup: guard (без запуска шифрования) ───────────────────────────────
async def test_job_backup_first_run_records_then_noop(services, fake_bot):
    assert services.db.get_state("last_backup") is None
    jobs = _jobs(services, fake_bot)
    await jobs["backup"]()                                   # первый запуск → только фиксация
    ym = timeutil.now().strftime("%Y-%m")
    assert services.db.get_state("last_backup") == ym
    await jobs["backup"]()                                   # тот же месяц → no-op (без send_document)
    assert not any(r[0] == "document" for r in fake_bot.records)


# ── job_poll: композиция + проглатывание ошибок ──────────────────────────────
async def test_job_poll_happy_sets_online_count(services, fake_bot, monkeypatch):
    monkeypatch.setattr(awg, "show_dump", lambda: [], raising=False)
    await _jobs(services, fake_bot)["poll"]()
    assert services.db.get_state("online_count") == "0"


async def test_job_poll_swallows_errors(services, fake_bot, monkeypatch):
    monkeypatch.setattr(services, "poll_traffic",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    await _jobs(services, fake_bot)["poll"]()                # не должно поднять исключение


# ── job_expiry: реальная композиция (истечение → уведомления) ────────────────
async def test_job_expiry_notifies_on_expiry(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=9003, period_kind="year")
    past = timeutil.to_iso(timeutil.now() - datetime.timedelta(days=1))
    services.db.update_client_fields(client.id, period_end=past)
    await _jobs(services, fake_bot)["expiry"]()
    assert any(r[0] == "send_message" and r[1] == 9003 for r in fake_bot.records)
    assert services.db.get_client(client.id).status == "expired"


# ── _service_failure_alerts: гистерезис громкого алерта ──────────────────────
def test_service_failure_alert_after_sustained_downtime(services):
    db = services.db
    assert _service_failure_alerts(db, ok=True) == []        # всё хорошо — тишина
    assert _service_failure_alerts(db, ok=False) == []       # первый сбой — только фиксируем
    # перематываем начало простоя за порог
    past = timeutil.now() - datetime.timedelta(minutes=config.SERVICE_FAILURE_ALERT_MINUTES + 1)
    db.set_state("service_down_since", timeutil.to_iso(past))
    alerts = _service_failure_alerts(db, ok=False)
    assert len(alerts) == 1 and alerts[0].force_sound is True
    assert _service_failure_alerts(db, ok=False) == []       # уже отправляли — не спамим
    assert _service_failure_alerts(db, ok=True) == []        # восстановление — сброс состояния
    assert db.get_state("service_alert_sent") == ""
