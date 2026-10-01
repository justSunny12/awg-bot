"""E2E: обёртки фоновых задач scheduler.py (замыкания job_*) и автомат простоя сервиса (services.service_failure_alerts).

Замыкания достаём из собранного планировщика через get_job(id).func и зовём
напрямую — без реального AsyncIOScheduler.start(). Проверяем guard-логику
месячного сброса/бэкапа (catch-up + защита от двойного), проглатывание ошибок
опросчиком и гистерезис громкого алерта простоя сервиса.
"""
import datetime

import pytest

from awgbot.runtime.scheduler import setup_scheduler
from awgbot.core import settings
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
    services.db.add_traffic_bulk([(dc.device_id, 100, 100)])
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
    services.db.add_traffic_bulk([(dc.device_id, 50, 50)])
    await _jobs(services, fake_bot)["monthly"]()
    assert services.db.get_device(dc.device_id).traffic_rx_month == 50   # не тронуто


async def test_job_monthly_catch_up_resets_after_downtime(services, fake_bot, make_active_client):
    client = make_active_client(tg_id=9002)
    dc = services.add_device(client.id, "d")
    services.db.set_state("last_monthly_reset", "2020-01")   # «проспали» границу месяца
    services.db.add_traffic_bulk([(dc.device_id, 70, 30)])
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
    monkeypatch.setattr(awg, "show_dump", lambda iface=None: [], raising=False)
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


# ── service_failure_alerts: гистерезис громкого алерта ──────────────────────
def test_service_failure_alert_after_sustained_downtime(services):
    db = services.db
    assert services.service_failure_alerts(ok=True) == []        # всё хорошо — тишина
    assert services.service_failure_alerts(ok=False) == []       # первый сбой — только фиксируем
    # перематываем начало простоя за порог
    past = timeutil.now() - datetime.timedelta(minutes=settings.get_int("app.monitoring.service_failure_alert_minutes", 5) + 1)
    db.set_state("service_down_since", timeutil.to_iso(past))
    alerts = services.service_failure_alerts(ok=False)
    assert len(alerts) == 1 and alerts[0].force_sound is True
    assert services.service_failure_alerts(ok=False) == []       # уже отправляли — не спамим
    assert services.service_failure_alerts(ok=True) == []        # восстановление — сброс состояния
    assert db.get_state("service_alert_sent") == ""


async def test_job_monitor_refreshes_the_view_key_written_by_the_refresh_button(services, fake_bot, monkeypatch):
    """«🔄 Обновить» пишет server_ok_view, панель читает его первым; раньше
    монитор его не трогал, и после первого нажатия шапка замерзала навсегда."""
    services.db.set_state("last_server_ok", "1")
    services.db.set_state("server_ok_view", "1")
    monkeypatch.setattr(services, "server_ok", lambda: False)
    await _jobs(services, fake_bot)["monitor"]()
    assert services.server_status_cached()["ok"] is False, "шапка показывает прошлое нажатие «Обновить»"


# ── обязательные задания: месячный сброс и копия ─────────────────────────────
def test_monthly_reset_and_backup_jobs_are_never_dropped(services, fake_bot):
    """Критичные задания без допуска по опозданию: APScheduler не выбрасывает
    их как просроченные, сколько бы ни длился старт."""
    sched = setup_scheduler(services, fake_bot, services.db)
    for jid in ("monthly", "monthly_catchup", "backup", "backup_catchup"):
        assert sched.get_job(jid).misfire_grace_time is None, jid


def test_planned_moment_of_the_month():
    import datetime as _dt
    from awgbot.runtime.scheduler import planned_moment_passed
    from awgbot.core import config
    tz = config.TZ
    assert planned_moment_passed(15, 12, ref=_dt.datetime(2026, 10, 3, 9, tzinfo=tz)) is False
    assert planned_moment_passed(15, 12, ref=_dt.datetime(2026, 10, 15, 12, 0, tzinfo=tz)) is True
    assert planned_moment_passed(15, 12, ref=_dt.datetime(2026, 10, 16, 9, tzinfo=tz)) is True
    assert planned_moment_passed(31, 0, ref=_dt.datetime(2026, 9, 30, 1, tzinfo=tz)) is True, "день короче месяца — последний"


async def test_backup_catchup_waits_for_the_planned_day(services, fake_bot, monkeypatch):
    """Рестарт 3-го при копии 15-го: догон не шлёт копию раньше срока и не
    помечает месяц сделанным, крон 15-го не пропускается."""
    from awgbot.runtime import scheduler as sm
    services.db.set_state("last_backup", "2000-01")
    services.db.set_state("last_monthly_reset", "2000-01")
    monkeypatch.setattr(sm, "planned_moment_passed", lambda day, hour, ref=None: False)
    sched = setup_scheduler(services, fake_bot, services.db)
    await sched.get_job("backup_catchup").func()
    assert services.db.get_state("last_backup") == "2000-01", "догон сделал копию раньше назначенного дня"
    await sched.get_job("monthly_catchup").func()
    assert services.db.get_state("last_monthly_reset") == "2000-01", "догон сбросил трафик раньше назначенного дня"
    monkeypatch.setattr(sm, "planned_moment_passed", lambda day, hour, ref=None: True)
    await sched.get_job("monthly_catchup").func()
    assert services.db.get_state("last_monthly_reset") == timeutil.now().strftime("%Y-%m")


async def test_an_undelivered_backup_is_retried_in_an_hour(services, fake_bot, monkeypatch, tmp_path):
    """Копия собрана, но не ушла ни почтой, ни в чат: повтор через час, задание
    обязательное — пока не уйдёт."""
    p = tmp_path / "b.tgz.enc"; p.write_bytes(b"x")
    services.db.set_state("last_backup", "2000-01")
    monkeypatch.setattr(services, "make_backup", lambda: [str(p)])
    monkeypatch.setattr(services, "backup_channel", lambda: "telegram")

    async def boom(*a, **k):
        raise RuntimeError("линк лежит")

    monkeypatch.setattr(fake_bot, "send_document", boom, raising=False)
    sched = setup_scheduler(services, fake_bot, services.db)
    await sched.get_job("backup").func()
    retry = sched.get_job("backup_retry")
    assert retry is not None and retry.misfire_grace_time is None, "повтора нет"
    assert services.db.get_state("last_backup") == "2000-01", "месяц помечен сделанным без доставки"
