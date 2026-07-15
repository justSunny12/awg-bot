"""Класс 2: смена расписания в settings перевешивает job APScheduler на лету
(reschedule_job), не пересоздавая процесс. Проверяем, что on-change хук,
зарегистрированный setup_scheduler, дёргает reschedule с новым триггером и только
для изменившегося job'а."""
import textwrap

import pytest

from awgbot.core import settings


@pytest.fixture
def sched_conf(tmp_path, monkeypatch):
    """conf/ c app.yaml (scheduler/history) + updates.yaml; settings на него.
    setup_scheduler зарегистрирует job'ы и on-change хуки; сам планировщик не
    запускаем (start() не зовём) — проверяем только reschedule_job."""
    (tmp_path / "app.yaml").write_text(textwrap.dedent("""\
        timezone: "Europe/Moscow"
        scheduler:
          traffic_poll_minutes: 5
          expiry_check_minutes: 60
          monitor_minutes: 3
          monthly_reset_day: 1
          monthly_reset_hour: 0
          backup_day: 1
          backup_hour: 12
        history:
          purge_hour: 3
    """), encoding="utf-8")
    (tmp_path / "updates.yaml").write_text("poll_hour: 10\npoll_minute: 0\n", encoding="utf-8")
    settings.init(tmp_path)
    yield tmp_path
    settings._on_change.clear()
    from awgbot.core import config
    settings.init(config.CONF_DIR)


def _build_scheduler(services, db):
    from awgbot.runtime import scheduler as sch
    bot = object()
    return sch.setup_scheduler(services, bot, db, watcher=None)


def test_monitor_interval_reschedules_hot(sched_conf, services, db, monkeypatch):
    scheduler = _build_scheduler(services, db)
    calls = []
    monkeypatch.setattr(scheduler, "reschedule_job",
                        lambda job_id, trigger=None: calls.append((job_id, trigger)))
    # меняем частоту монитора → хук должен перевесить только 'monitor'
    settings.set_value("app.scheduler.monitor_minutes", 7)
    assert len(calls) == 1
    job_id, trigger = calls[0]
    assert job_id == "monitor"
    assert "0:07:00" in str(trigger) or "interval" in str(trigger).lower()


def test_unchanged_job_not_touched(sched_conf, services, db, monkeypatch):
    scheduler = _build_scheduler(services, db)
    calls = []
    monkeypatch.setattr(scheduler, "reschedule_job",
                        lambda job_id, trigger=None: calls.append(job_id))
    # то же значение → diff пустой → ни одного reschedule
    settings.set_value("app.scheduler.monitor_minutes", 3)
    assert calls == []


def test_update_check_two_keys_one_job(sched_conf, services, db, monkeypatch):
    scheduler = _build_scheduler(services, db)
    calls = []
    monkeypatch.setattr(scheduler, "reschedule_job",
                        lambda job_id, trigger=None: calls.append(job_id))
    settings.set_value("updates.poll_hour", 8)
    settings.set_value("updates.poll_minute", 30)
    # оба ключа ведут на один job update_check — по разу за каждую правку
    assert calls == ["update_check", "update_check"]


def test_backup_cron_reschedules(sched_conf, services, db, monkeypatch):
    scheduler = _build_scheduler(services, db)
    calls = []
    monkeypatch.setattr(scheduler, "reschedule_job",
                        lambda job_id, trigger=None: calls.append((job_id, str(trigger))))
    settings.set_value("app.scheduler.backup_hour", 4)
    assert len(calls) == 1 and calls[0][0] == "backup"


def test_update_schedule_variants_and_never_pause(sched_conf, services, db, monkeypatch):
    """poll_schedule: week/month → reschedule; never → pause_job."""
    scheduler = _build_scheduler(services, db)
    resched, paused = [], []
    monkeypatch.setattr(scheduler, "reschedule_job",
                        lambda job_id, trigger=None: resched.append((job_id, str(trigger))))
    monkeypatch.setattr(scheduler, "pause_job", lambda job_id: paused.append(job_id))

    settings.set_value("updates.poll_schedule", "week")
    settings.set_value("updates.poll_schedule", "month")
    assert [j for j, _ in resched] == ["update_check"] * 2
    assert "day_of_week" in resched[0][1]           # week — по дню недели
    settings.set_value("updates.poll_schedule", "never")
    assert paused == ["update_check"]               # never — пауза, не reschedule


def test_legacy_hour_schedule_migrates_to_day(tmp_path, services, db, monkeypatch):
    """Снятый вариант poll_schedule=hour при построении расписания трактуется
    как day и однократно переписывается в YAML (чистая миграция)."""
    (tmp_path / "app.yaml").write_text(
        "timezone: \"Europe/Moscow\"\nscheduler:\n  monitor_minutes: 3\nhistory:\n  purge_hour: 3\n",
        encoding="utf-8")
    (tmp_path / "updates.yaml").write_text(
        'poll_schedule: "hour"\npoll_hour: 10\npoll_minute: 0\n', encoding="utf-8")
    settings.init(tmp_path)
    try:
        _build_scheduler(services, db)                 # регистрация зовёт _trig_update_check
        assert settings.get("updates.poll_schedule") == "day"      # мигрировано
        assert 'poll_schedule: "day"' in (tmp_path / "updates.yaml").read_text(encoding="utf-8")
    finally:
        settings._on_change.clear()
        from awgbot.core import config
        settings.init(config.CONF_DIR)
