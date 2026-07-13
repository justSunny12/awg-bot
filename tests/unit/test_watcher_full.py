"""Unit: логика inotify-вотчдога (runtime/watcher) без реального контейнера.

Мокаем watchdog.Observer, awg.container_pid, os.path.isdir/os.stat — проверяем
дебаунс→колбэк, фильтр событий, (пере)подключение, идемпотентность и стоп.
"""
import os
import threading
import time

import pytest

from awgbot.runtime import watcher as wmod
from awgbot.runtime.watcher import AwgWatcher
from awgbot.infra import awg

pytestmark = pytest.mark.unit


class FakeObserver:
    instances = []

    def __init__(self):
        self.started = False
        self._alive = True
        self.scheduled = []
        FakeObserver.instances.append(self)

    def schedule(self, handler, path, recursive=False):
        self.scheduled.append(path)

    def start(self):
        self.started = True

    def is_alive(self):
        return self._alive

    def stop(self):
        self._alive = False

    def join(self, timeout=None):
        pass


@pytest.fixture(autouse=True)
def _reset():
    FakeObserver.instances = []
    yield


# ── дебаунс → колбэк ─────────────────────────────────────────────────────────
def test_trigger_debounces_to_on_change():
    fired = threading.Event()
    w = AwgWatcher(on_change=fired.set, debounce=0.05)
    w._trigger()
    w._trigger()                                            # повторное событие — сбрасывает таймер
    assert fired.wait(0.5)                                  # сработало один раз после тишины
    w.stop()


def test_fire_swallows_callback_error():
    def boom():
        raise RuntimeError("x")
    w = AwgWatcher(on_change=boom, debounce=0.01)
    w._fire()                                               # не должно поднять исключение
    w.stop()


# ── фильтр событий ───────────────────────────────────────────────────────────
def test_handler_triggers_only_on_watched(monkeypatch):
    calls = []
    h = wmod._Handler(lambda: calls.append(1))
    monkeypatch.setattr(awg, "is_writing", lambda: False)

    class Ev:
        def __init__(self, src, dest=""):
            self.src_path, self.dest_path = src, dest
    h.on_any_event(Ev("/x/awg0.conf"))                      # watched → триггер
    h.on_any_event(Ev("/x/other.txt"))                      # не watched → тишина
    assert calls == [1]


def test_handler_skips_self_write(monkeypatch):
    calls = []
    h = wmod._Handler(lambda: calls.append(1))
    monkeypatch.setattr(awg, "is_writing", lambda: True)    # это наша запись

    class Ev:
        src_path = "/x/awg0.conf"
        dest_path = ""
    h.on_any_event(Ev())
    assert calls == []


# ── ensure_watching ──────────────────────────────────────────────────────────
def test_ensure_watching_binds_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(awg, "container_pid", lambda: 4242)
    monkeypatch.setattr(wmod, "Observer", FakeObserver)
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    monkeypatch.setattr(os, "stat", lambda p: type("S", (), {"st_mtime": 1.0})())
    w = AwgWatcher(on_change=lambda: None, debounce=0.01)
    w.ensure_watching()
    assert w.alive() is True
    assert len(FakeObserver.instances) == 1
    w.ensure_watching()                                    # тот же PID, наблюдатель жив → no-op
    assert len(FakeObserver.instances) == 1                # новый Observer не создан
    w.stop()
    assert w.alive() is False


def test_ensure_watching_no_pid_noops(monkeypatch):
    monkeypatch.setattr(awg, "container_pid", lambda: None)
    w = AwgWatcher(on_change=lambda: None)
    w.ensure_watching()
    assert w.alive() is False


def test_ensure_watching_missing_path(monkeypatch):
    monkeypatch.setattr(awg, "container_pid", lambda: 7)
    monkeypatch.setattr(os.path, "isdir", lambda p: False)   # /proc путь недоступен
    w = AwgWatcher(on_change=lambda: None)
    w.ensure_watching()
    assert w.alive() is False


# ── mtime-сетка ──────────────────────────────────────────────────────────────
def test_snapshot_mtimes(monkeypatch):
    w = AwgWatcher(on_change=lambda: None)
    w._pid = 99
    monkeypatch.setattr(os, "stat", lambda p: type("S", (), {"st_mtime": 123.0})())
    w._snapshot_mtimes()
    assert w._watched_paths() and all(v == 123.0 for v in w._mtimes.values())


def test_watched_paths_empty_without_pid():
    w = AwgWatcher(on_change=lambda: None)
    assert w._watched_paths() == []
