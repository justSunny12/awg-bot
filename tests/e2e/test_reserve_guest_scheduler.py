"""Планировщик бота основы на резерве (runtime/scheduler/guest.py): ровно
задания данных клиентов, три своих и два РФ-доступа в зоне (сверка
маршрутизации и живость слотов); хостовых заданий нет.

Цена ошибки: задание хоста (бэкапы, набор обновлений, метрики железа,
доставка шлюзам, доступ по SSH) в гостевом планировщике — бот основы на
резерве действует на хост резерва с данными основы: шлёт чужие бэкапы,
обновляет чужой сервер, правит чужой sshd; пропавшее задание данных клиентов
(сроки, месячный сброс) — клиенты основы на резерве не блокируются и не
получают уведомлений; тик канала, упавший с ошибкой наружу, останавливает
планировщик.
"""
from __future__ import annotations

import pytest

from awgbot.core import settings
from awgbot.runtime.scheduler import setup_guest_scheduler, setup_scheduler
from awgbot.runtime.scheduler.guest import GUEST_JOBS
from tests.conftest import restore_settings

pytestmark = pytest.mark.e2e

# задания данных клиентов основы + задача шифрованного снимка + монитор без хоста + такт канала
CORE = {"poll", "expiry", "monthly", "monthly_catchup", "purge_history", "email_resume", "tg_names", "tg_names_startup"}
OWN = {"guest_monitor", "guest_snapshot", "guest_ctl"}
# РФ-доступ клиентов основы в зоне — тем же кодом, что на основе
ROUTING = {"guest_routing", "routing_liveness"}
HOST_JOBS = {"backup", "monitor", "fleet", "gwjoin", "migration_watch", "ssh_access", "hostmetrics", "gw_delivery",
             "resource_alerts", "update_check"}


@pytest.fixture()
def guest_sched(services, fake_bot):
    sched = setup_guest_scheduler(services, fake_bot, services.db)
    yield sched
    restore_settings()


def _ids(sched) -> set[str]:
    return {j.id for j in sched.get_jobs()}


def test_the_guest_scheduler_has_exactly_the_client_data_jobs_its_own_ones_and_routing(guest_sched):
    want = CORE | OWN | ROUTING
    assert _ids(guest_sched) == want, f"лишнее/пропавшее: {_ids(guest_sched) ^ want}"
    assert set(GUEST_JOBS) == want, "перечень GUEST_JOBS разошёлся с планировщиком"


def test_no_host_job_is_scheduled_on_the_reserve(guest_sched, services, fake_bot):
    ids = _ids(guest_sched)
    assert not ids & HOST_JOBS, ids & HOST_JOBS
    main = setup_scheduler(services, fake_bot, services.db)
    try:
        host_only = {j.id for j in main.get_jobs()} - ids
        assert {"backup", "monitor"} <= host_only, "бэкапы и монитор хоста — только у основы"
    finally:
        restore_settings()


def test_the_data_jobs_of_the_guest_are_also_run_by_the_main(services, fake_bot):
    main = setup_scheduler(services, fake_bot, services.db)
    try:
        for jid in CORE - {"tg_names_startup"}:
            assert main.get_job(jid) is not None, f"задание {jid} пропало у основной роли после выноса в jobs.py"
    finally:
        restore_settings()


def test_the_intervals_follow_the_settings_of_the_main_that_came_in_the_package(services, fake_bot, monkeypatch):
    real = settings.get_int
    monkeypatch.setattr(settings, "get_int",
                        lambda key, default=None: 17 if key == "app.scheduler.traffic_poll_minutes" else real(key, default))
    sched = setup_guest_scheduler(services, fake_bot, services.db)
    try:
        assert sched.get_job("poll").trigger.interval.total_seconds() == 17 * 60
        assert sched.get_job("guest_ctl").trigger.interval.total_seconds() == 5
        assert sched.get_job("guest_snapshot").trigger.interval.total_seconds() == 30
    finally:
        restore_settings()


async def test_the_channel_tick_runs_the_callback_and_swallows_its_failure(services, fake_bot):
    seen = []

    async def tick():
        seen.append(1)
        raise RuntimeError("канал оборвался")
    sched = setup_guest_scheduler(services, fake_bot, services.db, on_ctl_tick=tick)
    try:
        await sched.get_job("guest_ctl").func()
        assert seen == [1], "тик канала не вызвал колбэк"
    finally:
        restore_settings()


async def test_the_channel_tick_without_a_callback_does_nothing(guest_sched):
    await guest_sched.get_job("guest_ctl").func()


async def test_the_snapshot_job_writes_through_the_service_and_swallows_failures(guest_sched, services, monkeypatch):
    calls = []
    monkeypatch.setattr(services, "guest_snapshot_tick", lambda *a, **k: calls.append(1) or True)
    await guest_sched.get_job("guest_snapshot").func()
    assert calls == [1]

    def boom(*a, **k):
        raise OSError("диск")
    monkeypatch.setattr(services, "guest_snapshot_tick", boom)
    await guest_sched.get_job("guest_snapshot").func()


async def test_the_guest_monitor_reconciles_blocks_and_records_the_clone_state(guest_sched, services, monkeypatch):
    done = []
    monkeypatch.setattr(services, "detect_and_handle_restart", lambda: False)
    monkeypatch.setattr(services, "reconcile_blocks", lambda *a, **k: done.append("blocks"))
    monkeypatch.setattr(services, "server_ok", lambda: True)
    await guest_sched.get_job("guest_monitor").func()
    assert done == ["blocks"] and services.db.get_state("last_server_ok") == "1"
    monkeypatch.setattr(services, "server_ok", lambda: False)
    await guest_sched.get_job("guest_monitor").func()
    assert services.db.get_state("last_server_ok") == "0"


async def test_after_a_clone_restart_the_monitor_does_not_reconcile_on_top(guest_sched, services, monkeypatch):
    done = []
    monkeypatch.setattr(services, "detect_and_handle_restart", lambda: True)
    monkeypatch.setattr(services, "reconcile_blocks", lambda *a, **k: done.append("blocks"))
    monkeypatch.setattr(services, "server_ok", lambda: True)
    await guest_sched.get_job("guest_monitor").func()
    assert done == [], "после перезапуска блокировки накладывает detect_and_handle_restart"


async def test_a_failing_monitor_step_does_not_stop_the_other_one(guest_sched, services, monkeypatch):
    monkeypatch.setattr(services, "detect_and_handle_restart", lambda: (_ for _ in ()).throw(OSError("нет iptables")))
    monkeypatch.setattr(services, "server_ok", lambda: True)
    await guest_sched.get_job("guest_monitor").func()
    assert services.db.get_state("last_server_ok") == "1"


# ── РФ-доступ в зоне ────────────────────────────────────────────────────────

async def test_the_routing_job_reconciles_and_sends_the_infra_alerts(guest_sched, services, fake_bot, monkeypatch):
    from awgbot.domain.services.types import Notification
    from awgbot.core import config
    done = []
    monkeypatch.setattr(services, "reconcile_routing", lambda *a, **k: done.append("reconcile"))
    monkeypatch.setattr(services, "routing_infra_alerts",
                        lambda: [Notification(tg_id=config.ADMIN_ID, text="🔴 списки РФ-доступа не применились")])
    await guest_sched.get_job("guest_routing").func()
    assert done == ["reconcile"], "сверка маршрутизации гостя не запущена"
    assert [r for r in fake_bot.records if r[0] == "send_message" and "списки РФ-доступа" in r[2]], fake_bot.records


async def test_a_failing_routing_job_does_not_raise_into_the_scheduler(guest_sched, services, monkeypatch):
    monkeypatch.setattr(services, "reconcile_routing", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nft")))
    await guest_sched.get_job("guest_routing").func()
    monkeypatch.setattr(services, "routing_liveness_tick", lambda: (_ for _ in ()).throw(RuntimeError("зонд")))
    await guest_sched.get_job("routing_liveness").func()


async def test_the_liveness_job_ticks_the_slots_and_sends_its_notes(guest_sched, services, fake_bot, monkeypatch):
    from awgbot.domain.services.types import Notification
    from awgbot.core import config
    monkeypatch.setattr(services, "routing_liveness_tick",
                        lambda: [Notification(tg_id=config.ADMIN_ID, text="⚠️ РФ-доступ переключён на шлюзы «Gamma»")])
    await guest_sched.get_job("routing_liveness").func()
    assert [r for r in fake_bot.records if r[0] == "send_message" and "шлюзы «Gamma»" in r[2]], fake_bot.records


def test_the_liveness_job_follows_probe_seconds_with_jitter(services, fake_bot, monkeypatch):
    real = settings.get_int
    monkeypatch.setattr(settings, "get_int",
                        lambda key, default=None: 40 if key == "app.routing.probe_seconds" else real(key, default))
    sched = setup_guest_scheduler(services, fake_bot, services.db)
    try:
        trig = sched.get_job("routing_liveness").trigger
        assert trig.interval.total_seconds() == 40 and trig.jitter and trig.jitter >= 2, \
            "живость слотов без джиттера — зонды наружу строго периодичны"
    finally:
        restore_settings()
