"""Планировщик основного бота в следовании (runtime/scheduler: FOLLOWER_OFF,
following): пока пользователи и бот основы у резерва, задания решений над
данными пользователей выходят сразу — сроки и паузы, месячный сброс и его
догон, почта выхода из паузы, имена аккаунтов, окно переезда профилей; опрос
трафика идёт (счётчики), но без лимитов и уведомлений; монитор не докладывает
готовность переезда.

Замыкания берутся из собранного планировщика (get_job(id).func) и зовутся
напрямую; каждое задание проверяется дважды — в следовании (ничего не
делает) и у держателя (делает) — иначе подмена, которую задание не зовёт,
выглядела бы как «вышло сразу».

Цена ошибки: задание решений, работающее в следовании, — основа по
устаревшей базе истекает продлённые на резерве подписки, сбрасывает месяц
второй раз, снимает паузу по коду, который уже прочитал бот на резерве, и
шлёт людям уведомления, которых они не должны были получить; выключенный
опрос трафика — расход тех, кто ещё сидит на основе, теряется; догон
месячного сброса только на старте — сброс, отложенный следованием, встаёт
лишь со следующим перезапуском.
"""
from __future__ import annotations

import pytest

from awgbot.domain.services.types import Notification
from awgbot.infra import awg
from awgbot.runtime import scheduler as schedpkg
from awgbot.runtime.scheduler import FOLLOWER_OFF, setup_scheduler
from awgbot.util import timeutil
from tests.conftest import restore_settings

pytestmark = pytest.mark.e2e

# задания решений над данными пользователей (как CORE у гостевого планировщика),
# без опроса трафика и очистки истории, плюс окно переезда профилей
WANT_OFF = {"expiry", "monthly", "monthly_catchup", "email_resume", "tg_names", "tg_names_startup", "migration_watch"}


def follow(db) -> None:
    db.jrn_pass(holder="peer", role="follower", why="тест: следование")


@pytest.fixture()
def sched(services, fake_bot):
    s = setup_scheduler(services, fake_bot, services.db)
    yield s
    restore_settings()


class _Spy:
    def __init__(self, ret=None):
        self.calls = 0
        self.ret = ret

    def __call__(self, *a, **k):
        self.calls += 1
        return self.ret() if callable(self.ret) else self.ret


def _arm(job: str, services, monkeypatch) -> _Spy:
    """Подмена того, что задание делает по существу; возвращает счётчик вызовов."""
    if job == "expiry":
        spy = _Spy(ret=list)
        monkeypatch.setattr(services, "check_expiry", spy)
        monkeypatch.setattr(services, "check_pauses", lambda: [])
    elif job in ("monthly", "monthly_catchup"):
        services.db.set_state("last_monthly_reset", "2020-01")       # месяц не сброшен — заданию есть что делать
        spy = _Spy(ret=list)
        monkeypatch.setattr(services, "reset_monthly_traffic", spy)
    elif job == "email_resume":
        spy = _Spy(ret=False)
        monkeypatch.setattr(services, "email_resume_enabled", spy)
    elif job in ("tg_names", "tg_names_startup"):
        spy = _Spy()

        async def refresh(bot, db, *a, **k):
            spy()
            return 0
        monkeypatch.setattr(schedpkg, "refresh_tg_names", refresh)
    elif job == "migration_watch":
        spy = _Spy(ret=list)
        monkeypatch.setattr(services, "migration_watch", spy)
    else:
        raise AssertionError(f"нет подмены для {job}")
    return spy


def test_the_follower_off_list_matches_the_scheduler(sched):
    """Сторож: перечень FOLLOWER_OFF — ровно задания решений, и каждое есть в
    планировщике основного бота (переименованное задание выпало бы из
    следования молча)."""
    assert set(FOLLOWER_OFF) == WANT_OFF, f"лишнее/пропавшее: {set(FOLLOWER_OFF) ^ WANT_OFF}"
    ids = {j.id for j in sched.get_jobs()}
    assert set(FOLLOWER_OFF) <= ids, f"нет в планировщике: {set(FOLLOWER_OFF) - ids}"
    assert {"poll", "purge_history", "monitor"} <= ids - set(FOLLOWER_OFF), \
        "опрос трафика, очистка истории и монитор в следовании идут"


@pytest.mark.parametrize("job", sorted(WANT_OFF))
async def test_every_follower_off_job_exits_at_once_while_following(sched, services, fake_bot, monkeypatch, job):
    spy = _arm(job, services, monkeypatch)
    follow(services.db)
    await sched.get_job(job).func()
    assert spy.calls == 0, f"{job}: в следовании задание решений отработало"
    assert not [r for r in fake_bot.records if r[0] == "send_message"], f"{job}: уведомление из следования"


@pytest.mark.parametrize("job", sorted(WANT_OFF))
async def test_the_same_jobs_do_their_work_at_the_holder(sched, services, monkeypatch, job):
    """Контроль подмены: у держателя то же задание доходит до дела."""
    spy = _arm(job, services, monkeypatch)
    await sched.get_job(job).func()
    assert spy.calls >= 1, f"{job}: подмена не задета — проверка следования выше ничего не доказывает"


async def test_the_monthly_reset_falls_on_the_follower_is_put_off_not_lost(sched, services, monkeypatch):
    """Сброс месяца в следовании не делается и не отмечается как сделанный —
    откладывается (его делает держатель); отметка месяца остаётся прежней."""
    services.db.set_state("last_monthly_reset", "2020-01")
    spy = _Spy(ret=list)
    monkeypatch.setattr(services, "reset_monthly_traffic", spy)
    follow(services.db)
    await sched.get_job("monthly").func()
    ym = timeutil.now().strftime("%Y-%m")
    assert spy.calls == 0
    assert services.db.get_state("last_monthly_reset") == "2020-01", "месяц отмечен сброшенным, а сброса не было"
    assert services.rsv_get("rsv_follow").get("monthly_deferred") == ym, "отложенный сброс не запомнен"


async def test_the_traffic_poll_keeps_counting_but_sends_and_limits_nothing(sched, services, fake_bot,
                                                                         make_active_client, monkeypatch):
    """Опрос трафика в следовании пишет счётчики своего интерфейса, но не зовёт
    лимиты и не шлёт поздравлений; у держателя — то и другое."""
    ivan = make_active_client(name="Иван", tg_id=7100)
    dc = services.add_device(ivan.id, "Телефон")
    pub = services.db.get_device(dc.device_id).public_key
    rx = {"v": 1000}
    monkeypatch.setattr(awg, "show_dump", lambda iface=None: [{"public_key": pub, "rx": rx["v"], "tx": rx["v"],
                                                                "last_handshake": 1_700_000_000}], raising=False)
    real_poll = services.poll_traffic
    monkeypatch.setattr(services, "poll_traffic", lambda: real_poll() + [Notification(7100, "привет с новых настроек")])
    limits = _Spy(ret=list)
    monkeypatch.setattr(services, "check_traffic_limits", limits)
    job = sched.get_job("poll").func
    await job()                                                # первая база сэмпла — как у держателя
    follow(services.db)
    rx["v"] = 5000
    await job()
    dev = services.db.get_device(dc.device_id)
    assert int(dev.traffic_rx_month) == 4000, "следующий не учёл расход своего интерфейса"
    assert limits.calls == 1, "лимиты проверены в следовании (первый вызов — у держателя)"
    texts = [r for r in fake_bot.records if r[0] == "send_message"]
    assert len(texts) == 1, f"поздравление из следования ушло человеку: {texts}"


async def test_the_monitor_does_not_announce_the_migration_window_while_following(sched, services, monkeypatch):
    spy = _Spy(ret=list)
    monkeypatch.setattr(services, "migration_ready_alerts", spy)
    monkeypatch.setattr(services, "detect_and_handle_restart", lambda: False)
    await sched.get_job("monitor").func()
    assert spy.calls == 1, "контроль: у держателя монитор докладывает готовность"
    follow(services.db)
    await sched.get_job("monitor").func()
    assert spy.calls == 1, "монитор следующего поставил отметку доклада готовности переезда"


def test_the_monthly_catch_up_runs_at_start_and_every_hour(sched):
    """Догон сброса — на старте и раз в час: сброс, отложенный следованием и не
    учтённый слиянием, встаёт вскоре после возврата."""
    job = sched.get_job("monthly_catchup")
    assert job.next_run_time is not None, "догон на старте не назначен"
    assert getattr(job.trigger, "interval", None) is not None and job.trigger.interval.total_seconds() == 3600, \
        f"догон не раз в час: {job.trigger}"
