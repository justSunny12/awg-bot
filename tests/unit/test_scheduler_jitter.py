"""
Сторож планировщиков обеих ролей: у каждой задачи, которая ходит в сеть
(зонд, списки, фиды, проверка обновлений, месячная копия, имена аккаунтов),
есть джиттер — строго периодический запрос выглядит маячком (правило
проекта); у агента есть догон месячной копии на старте.
"""
from __future__ import annotations

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

pytestmark = pytest.mark.unit

# задачи, которым в сеть ходить по расписанию — и потому обязателен джиттер
NETWORK_JOBS_MAIN = ("monitor", "routing_liveness", "backup", "update_check", "tg_names")
NETWORK_JOBS_GW = ("gw_monitor", "gw_lan_lists", "gw_lan_services", "gw_backup", "update_check")


def _jitter(trigger) -> int:
    assert isinstance(trigger, (IntervalTrigger, CronTrigger)), type(trigger)
    return int(trigger.jitter or 0)


async def test_every_network_job_of_the_agent_has_jitter_and_the_backup_catches_up(tmp_path, monkeypatch):
    from awgbot.core import config
    from awgbot.domain.gateway import GatewayServices
    from awgbot.infra.db import Database
    from awgbot.runtime import scheduler as sch
    monkeypatch.setattr(config, "ROLE", "gateway")
    db = Database(tmp_path / "gw.db"); db.init_schema()
    scheduler = sch.setup_gateway_scheduler(GatewayServices(db), object())
    try:
        jobs = {j.id: j for j in scheduler.get_jobs()}
        for jid in NETWORK_JOBS_GW:
            assert jid in jobs, f"у агента нет задачи {jid}"
            assert _jitter(jobs[jid].trigger) > 0, f"{jid}: без джиттера — маячок по расписанию"
        assert isinstance(jobs["gw_backup_catchup"].trigger, DateTrigger), "нет догона месячной копии на старте"
        assert isinstance(jobs["update_check_startup"].trigger, DateTrigger)
        assert _jitter(jobs["gw_backup"].trigger) == sch.BACKUP_JITTER
    finally:
        scheduler.shutdown(wait=False)
        db.close()


async def test_every_network_job_of_the_main_bot_has_jitter(services, db, monkeypatch):
    from awgbot.core import config
    from awgbot.runtime import scheduler as sch
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    scheduler = sch.setup_scheduler(services, object(), db, watcher=None)
    try:
        jobs = {j.id: j for j in scheduler.get_jobs()}
        for jid in NETWORK_JOBS_MAIN:
            assert jid in jobs, f"нет задачи {jid}"
            assert _jitter(jobs[jid].trigger) > 0, f"{jid}: без джиттера — маячок по расписанию"
        assert isinstance(jobs["backup_catchup"].trigger, DateTrigger)
        assert _jitter(jobs["backup"].trigger) == sch.BACKUP_JITTER
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
