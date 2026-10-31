"""Остановка гостя сигналом юнита (runtime/main/guest.py: run_guest, _wait_stop).

`systemctl stop`/перезапуск юнита гостя шлёт SIGTERM; без обработчика `asyncio.run`
завершал процесс без `finally` — финальный снимок `rep.bin` (ветка «остановка не по
команде») не писался, и возврат шёл по снимку 30-секундного такта: последние правки
на резерве терялись. Теперь сигнал будит ожидание рядом с командой `stop` локального
канала, снимок пишется, слушатель и канал закрываются.

Окружение — заглушки всего, что собирает run_guest (база, сервисы, бот, канал,
планировщик); обработчик сигнала перехватывается у цикла событий и зовётся как
сигнал.

Цена ошибки: правки пользователей за последние полминуты обслуживания на резерве
пропадают при каждом перезапуске юнита гостя.
"""
from __future__ import annotations

import asyncio
import signal
import types

import pytest

from awgbot.runtime import main as main_pkg
from awgbot.runtime.main import guest as guest_mod

pytestmark = pytest.mark.unit


class _Db:
    path = ":memory:"

    def __init__(self, path=""):
        pass

    def init_schema(self):
        pass

    def close(self):
        pass

    def normalize_default_iface(self, iface):
        return 0


class _Services:
    def __init__(self, db):
        self.db = db
        self.snapshots: list[bool] = []
        self.bot_username = ""
        self.bot_name = ""

    def guest_adopt_snapshot(self):
        return False

    def guest_install_refusals(self):
        return []

    def guest_snapshot_hook_install(self):
        pass

    def ensure_admin_client(self):
        pass

    def guest_hook_foreign_load(self):
        return False

    def gwlink_sessions_reset(self):
        pass

    def reserve_quorum_guest_start(self):
        pass

    def detect_and_handle_restart(self):
        pass

    def guest_jrn_on(self):
        return False

    def poll_traffic(self):
        pass

    def guest_snapshot_bytes(self):
        return b"\x01"

    def guest_snapshot_tick(self, final=False):
        self.snapshots.append(bool(final))


class _Session:
    async def close(self):
        pass


class _Bot:
    def __init__(self, *a, **k):
        self.session = _Session()

    async def get_me(self):
        return types.SimpleNamespace(username="primary_bot", first_name="Основа")


class _Ctl:
    def __init__(self, services):
        self.stop_req = asyncio.Event()
        self.take_tg = asyncio.Event()
        self.tg_acked = asyncio.Event()
        self.sent: list = []
        self.stopped = False

    def start(self):
        pass

    def connected(self):
        return False

    async def send(self, kind, body=None):
        self.sent.append(kind)
        return True

    async def send_replica(self, blob):
        self.sent.append("replica")
        return True

    async def stop(self):
        self.stopped = True


class _Scheduler:
    def start(self):
        pass

    def shutdown(self, wait=False):
        pass


@pytest.fixture()
def world(monkeypatch):
    from awgbot.runtime import quorum, scheduler, tgsession
    from awgbot.runtime.pairlink import guestctl
    from awgbot.bot.lists import msgs
    w = types.SimpleNamespace(services=None, ctl=None, handlers={})
    monkeypatch.setattr(main_pkg, "Database", _Db)

    def mk_services(db):
        w.services = _Services(db)
        return w.services

    def mk_ctl(services):
        w.ctl = _Ctl(services)
        return w.ctl
    monkeypatch.setattr(main_pkg, "Services", mk_services)
    monkeypatch.setattr(main_pkg, "Bot", _Bot)
    monkeypatch.setattr(main_pkg, "make_dispatcher", lambda *a, **k: object())

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(main_pkg, "_menu_command", _noop)
    monkeypatch.setattr(main_pkg, "do_reconcile", _noop)
    monkeypatch.setattr(tgsession, "make", lambda: None)
    monkeypatch.setattr(msgs, "attach", lambda bot, services: None)
    monkeypatch.setattr(guestctl, "GuestCtlClient", mk_ctl)
    monkeypatch.setattr(guestctl, "set_client", lambda ctl: None)
    monkeypatch.setattr(scheduler, "setup_guest_scheduler", lambda *a, **k: _Scheduler())
    monkeypatch.setattr(quorum, "shutdown", _noop)
    monkeypatch.setattr("awgbot.runtime.webhook.load_conf", lambda: None)
    return w


async def _started(w) -> asyncio.Task:
    """run_guest — задачей; обработчики сигналов у цикла перехвачены в w.handlers."""
    loop = asyncio.get_running_loop()
    loop.add_signal_handler = lambda sig, cb, *a: w.handlers.__setitem__(sig, cb)   # type: ignore[method-assign]
    loop.remove_signal_handler = lambda sig: w.handlers.pop(sig, None) is not None  # type: ignore[method-assign]
    task = asyncio.create_task(guest_mod.run_guest())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if w.ctl is not None and signal.SIGTERM in w.handlers:
            return task
    task.cancel()
    raise AssertionError("гость не дошёл до ожидания команды")


async def test_sigterm_writes_the_final_snapshot_and_closes_the_channel(world):
    task = await _started(world)
    world.handlers[signal.SIGTERM]()
    await asyncio.wait_for(task, 5)
    assert world.services.snapshots == [True], "финальный снимок rep.bin по сигналу не записан"
    assert world.ctl.stopped and "replica" not in world.ctl.sent, "по сигналу реплика боту резерва не идёт"
    assert signal.SIGTERM not in world.handlers, "обработчик сигнала не снят"


async def test_the_stop_command_still_sends_the_replica_without_a_second_snapshot(world):
    task = await _started(world)
    world.ctl.stop_req.set()
    await asyncio.wait_for(task, 5)
    assert world.services.snapshots == [True] and world.ctl.sent[-2:] == ["replica", "bye"]


async def test_wait_stop_tells_a_signal_from_a_command(world):
    loop = asyncio.get_running_loop()
    loop.add_signal_handler = lambda sig, cb, *a: world.handlers.__setitem__(sig, cb)   # type: ignore[method-assign]
    loop.remove_signal_handler = lambda sig: world.handlers.pop(sig, None) is not None  # type: ignore[method-assign]
    ctl = _Ctl(None)
    waiting = asyncio.create_task(guest_mod._wait_stop(ctl))
    await asyncio.sleep(0.01)
    world.handlers[signal.SIGINT]()
    assert await asyncio.wait_for(waiting, 2) is False
    ctl.stop_req.set()
    assert await asyncio.wait_for(guest_mod._wait_stop(ctl), 2) is True
