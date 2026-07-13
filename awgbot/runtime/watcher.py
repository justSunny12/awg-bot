"""
watcher.py — inotify-вотчдог за файлами Amnezia (awg0.conf, clientsTable).

Файлы живут ВНУТРИ контейнера; наблюдаем их с хоста через /proc/<PID>/root/…
(разведка показала: bind-mount'а нет, но /proc-путь стабилен в пределах жизни
контейнера). PID меняется при рестарте → ensure_watching() ребайндит (зовётся
из мониторинг-задачи планировщика).

Три тонкости, подтверждённые разведкой:
  • дебаунс (приложение пишет 2 файла с разбежкой в секунды) — ждём тишины;
  • игнор самозаписи — awg.is_writing() True, пока бот сам правит файлы;
  • ребайнд на новый PID после рестарта контейнера.

ВНИМАНИЕ: inotify через /proc/<PID>/root требует проверки на живом сервере.
Если события не приходят (namespace-нюансы) — fallback на опрос mtime (см.
PollingWatcher ниже), включается флагом.
"""

from __future__ import annotations

import logging
import os
import threading
import time as _time
from typing import Callable, Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from awgbot.infra import awg
from awgbot.core import config

log = logging.getLogger("awgbot.watcher")

_WATCHED_NAMES = {"awg0.conf", "clientsTable"}


class _Handler(FileSystemEventHandler):
    def __init__(self, trigger: Callable[[], None]):
        self._trigger = trigger

    def on_any_event(self, event):
        if awg.is_writing():                         # это наша собственная запись
            return
        name = os.path.basename(getattr(event, "src_path", "") or "")
        dest = os.path.basename(getattr(event, "dest_path", "") or "")
        if name in _WATCHED_NAMES or dest in _WATCHED_NAMES:
            self._trigger()


class AwgWatcher:
    """Гибридный наблюдатель: inotify (миллисекунды) + страховочная mtime-сетка.

    inotify через /proc/<PID>/root теоретически может не пробить namespace/overlayfs
    на конкретном ядре — поэтому параллельно раз в POLL_NET_SECONDS сверяем mtime
    файлов через os.stat по тому же /proc-пути. Это СИСТЕМНЫЙ ВЫЗОВ с хоста
    (микросекунды, ноль сабпроцессов), не docker exec. Худшая задержка при
    неработающем inotify — один интервал сетки; при работающем — сетка молчит
    (mtime уже совпадает к моменту её прохода после дебаунса реконсиляции).

    on_change — thread-safe колбэк (вызывается после дебаунса).
    """

    POLL_NET_SECONDS = 10

    def __init__(self, on_change: Callable[[], None],
                 debounce: float = config.WATCHER_DEBOUNCE_SECONDS):
        self._on_change = on_change
        self._debounce = debounce
        self._observer: Optional[Observer] = None
        self._pid: Optional[int] = None
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._mtimes: dict[str, float] = {}
        self._net_stop = threading.Event()
        self._net_thread: Optional[threading.Thread] = None

    def alive(self) -> bool:
        """Жив ли inotify-наблюдатель (для монитора: ребайнд только по надобности).
        mtime-сетка живёт отдельным потоком и не учитывается — она страховка."""
        return self._observer is not None and self._observer.is_alive()

    def ensure_watching(self) -> None:
        """(Пере)подключает наблюдение к /proc/<PID>/root<AWG_DIR>. Идемпотентно:
        если PID не изменился и наблюдатель жив — ничего не делает."""
        pid = awg.container_pid()
        if pid is None:
            return
        if (pid == self._pid and self._observer is not None
                and self._observer.is_alive()):
            return
        self._stop_observer()
        path = f"/proc/{pid}/root{config.AWG_DIR}"
        if not os.path.isdir(path):
            log.warning("Путь наблюдения недоступен: %s", path)
            return
        self._pid = pid
        self._snapshot_mtimes()                      # база для сетки под новый PID
        try:
            obs = Observer()
            obs.schedule(_Handler(self._trigger), path, recursive=False)
            obs.start()
            self._observer = obs
            log.info("Вотчдог подключён к %s (pid=%s)", path, pid)
        except Exception as e:                       # noqa: BLE001
            log.warning("Не удалось запустить inotify (работает mtime-сетка): %s", e)
        if self._net_thread is None:
            self._net_thread = threading.Thread(
                target=self._net_loop, daemon=True, name="awg-mtime-net")
            self._net_thread.start()

    # ── страховочная mtime-сетка ─────────────────────────────────────────────

    def _watched_paths(self) -> list[str]:
        if self._pid is None:
            return []
        base = f"/proc/{self._pid}/root{config.AWG_DIR}"
        return [os.path.join(base, name) for name in _WATCHED_NAMES]

    def _snapshot_mtimes(self) -> None:
        for p in self._watched_paths():
            try:
                self._mtimes[p] = os.stat(p).st_mtime
            except OSError:
                self._mtimes.pop(p, None)

    def _net_loop(self) -> None:
        while not self._net_stop.wait(self.POLL_NET_SECONDS):
            try:
                changed = False
                for p in self._watched_paths():
                    try:
                        m = os.stat(p).st_mtime
                    except OSError:                  # PID устарел — ждём ребайнда
                        continue
                    if self._mtimes.get(p) != m:
                        self._mtimes[p] = m
                        changed = True
                if not changed:
                    continue
                # это наша собственная запись? (идёт сейчас или только что закончилась)
                if awg.is_writing() or (
                        _time.time() - awg.last_self_write()
                        < self.POLL_NET_SECONDS + self._debounce):
                    continue                          # mtime уже обновлён выше — молчим
                self._trigger()
            except Exception as e:                   # noqa: BLE001
                log.debug("mtime-сетка: %s", e)

    def _trigger(self) -> None:
        """Дебаунс: перезапускаем таймер на каждое событие, срабатываем в тишине."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        try:
            self._on_change()
        except Exception as e:                       # noqa: BLE001
            log.warning("on_change: %s", e)

    def _stop_observer(self) -> None:
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=3)
            except Exception:                        # noqa: BLE001
                pass
            self._observer = None

    def stop(self) -> None:
        self._net_stop.set()
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
        self._stop_observer()


__all__ = ["AwgWatcher"]
