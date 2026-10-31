"""Вебхук гостя после рестарта юнита (runtime/main/guest.py: такт локального канала
`ctl_tick` в run_guest).

Отметка `tg_take` лежит на диске зоны и переживает рестарт гостя; по ней одной гость
ставил вебхук — и отнимал Telegram у основы, которая его уже забрала. Теперь такт ставит
вебхук только по слову бота резерва (`tg{go}` — `ctl.tg_go`), а при отметке «вебхук не
здесь» в хранилище гостя — только по явному (`tg{go, foreign}`).

Окружение — заглушки всего, что собирает run_guest (как в test_guest_sigterm), слушатель
вебхука подставной; решение — настоящий `guest_tg_may_take` поверх отметки в хранилище
(пути зоны — временные, conftest).

Цена ошибки: гость после рестарта ставит вебхук поверх основы — два бота на одном
токене, решения пишут обе стороны; слово, не доходящее до такта, — пользователи на
резерве без бота.
"""
from __future__ import annotations

import asyncio
import signal

import pytest

from awgbot.domain.services.reserve.guest import ReserveGuestMixin
from awgbot.infra import rsvzone
from awgbot.runtime import main as main_pkg
from awgbot.runtime.main import guest as guest_mod

from tests.unit import test_guest_sigterm as base
from tests.unit.test_guest_sigterm import world

pytestmark = pytest.mark.unit


class _Services(base._Services):
    """Заглушка сервисов гостя с настоящим решением о вебхуке и отметкой в хранилище."""
    guest_hook_foreign_load = ReserveGuestMixin.guest_hook_foreign_load
    guest_hook_foreign = ReserveGuestMixin.guest_hook_foreign
    guest_tg_may_take = ReserveGuestMixin.guest_tg_may_take

    def guest_can_take_tg(self):
        return True

    def guest_state(self):
        return {}


class _Wh:
    def __init__(self, *a, **k):
        self.set_calls = 0

    def refresh_pool(self):
        return 0

    async def start(self, dp, bot):
        pass

    async def stop(self):
        pass

    async def set_webhook(self, bot, dp):
        self.set_calls += 1
        return True


__all__ = ["world"]


@pytest.fixture()
def gw(world, monkeypatch):
    w = world
    w.tick = None
    w.wh = None

    def mk_services(db):
        w.services = _Services(db)
        return w.services
    monkeypatch.setattr(main_pkg, "Services", mk_services)
    from awgbot.runtime import scheduler, webhook

    def mk_sched(services, bot, db, on_ctl_tick=None):
        w.tick = on_ctl_tick
        return base._Scheduler()

    def mk_wh(conf, pool):
        w.wh = _Wh()
        return w.wh
    monkeypatch.setattr(scheduler, "setup_guest_scheduler", mk_sched)
    monkeypatch.setattr(webhook, "load_conf", lambda: object())
    monkeypatch.setattr(webhook, "load_pool", lambda: [])
    monkeypatch.setattr(webhook, "WebhookServer", mk_wh)
    orig = base._Ctl.__init__

    def ctl_init(self, services):
        orig(self, services)
        self.tg_go = False
        self.tg_foreign_ack = False
    monkeypatch.setattr(base._Ctl, "__init__", ctl_init)
    rsvzone.TG_TAKE_FILE.parent.mkdir(parents=True, exist_ok=True)
    rsvzone.TG_TAKE_FILE.write_text("1")                     # отметка прошлого запуска на диске
    return w


async def _run(w):
    task = await base._started(w)
    assert w.tick is not None and w.wh is not None
    return task


async def _end(w, task):
    w.handlers[signal.SIGTERM]()
    await asyncio.wait_for(task, 5)


async def test_the_disk_mark_alone_does_not_set_the_webhook_after_a_restart(gw):
    task = await _run(gw)
    try:
        await gw.tick()
        assert gw.wh.set_calls == 0, "вебхук по одной отметке tg_take — поверх основы"
        gw.ctl.tg_go = True
        await gw.tick()
        assert gw.wh.set_calls == 1, "слово бота резерва не дало вебхук"
    finally:
        await _end(gw, task)


async def test_the_foreign_mark_of_the_last_run_needs_the_explicit_word(gw):
    rsvzone.write_private(rsvzone.HOOK_FOREIGN_FILE, "2026-11-01T10:00:00+05:00")
    task = await _run(gw)
    try:
        assert gw.services.guest_hook_foreign(), "отметка прошлого запуска не подхвачена на старте"
        gw.ctl.tg_go = True
        await gw.tick()
        assert gw.wh.set_calls == 0, "при отметке «вебхук не здесь» — по обычному слову"
        gw.ctl.tg_foreign_ack = True
        await gw.tick()
        assert gw.wh.set_calls == 1
    finally:
        await _end(gw, task)
