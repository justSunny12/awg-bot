"""conf_watcher.py — вотчдог за каталогом conf/ (горячая правка настроек).

Отдельный от watcher.py (тот следит за файлами Amnezia ВНУТРИ контейнера через
/proc/<PID>/root и реагирует syncconf'ом). Здесь — обычный каталог хоста
conf/*.yaml, реакция иная: settings.reload() с пофайловым diff и on-change только
по изменившимся ключам. Разные пути, разная реакция → раздельные вотчдоги, чтобы
не мешать рабочий watcher.

Игнорируем собственные записи (settings.is_self_writing()) — правка из чат-кнопки
не должна выглядеть внешним изменением и запускать лишний цикл. Дебаунс на пачку
событий (редакторы пишут в несколько шагов: temp+rename).
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from awgbot.core import settings

log = logging.getLogger("awgbot.conf_watcher")

_DEBOUNCE_SEC = 1.0


class _Handler(FileSystemEventHandler):
    def __init__(self):
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def _is_yaml(self, path: str) -> bool:
        return path.endswith(".yaml")

    def _schedule(self):
        # Самозапись settings.set_value: событие в пределах жизни флага —
        # пропускаем. Событие может прилететь и ПОСЛЕ снятия флага (он живёт
        # миллисекунды) — тогда случится «лишний» reload, но он безвреден:
        # кэш уже обновлён set_value, diff пуст, хуки не дёргаются.
        if settings.is_self_writing():
            return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(_DEBOUNCE_SEC, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        try:
            changed = settings.reload()
            if changed:
                log.info("conf/: горячее применение, ключей: %d", len(changed))
        except Exception as e:                                # noqa: BLE001
            log.warning("conf_watcher reload упал: %s", e)

    def on_modified(self, event):
        if not event.is_directory and self._is_yaml(event.src_path):
            self._schedule()

    def on_created(self, event):
        if not event.is_directory and self._is_yaml(event.src_path):
            self._schedule()

    def on_moved(self, event):
        # temp+rename при атомарной записи прилетает как moved
        dest = getattr(event, "dest_path", "")
        if self._is_yaml(dest):
            self._schedule()


class ConfWatcher:
    """Наблюдает conf_dir и горячо применяет изменения через settings.reload()."""

    def __init__(self, conf_dir):
        self._dir = str(Path(conf_dir))
        self._observer: Optional[Observer] = None

    def start(self) -> None:
        try:
            self._observer = Observer()
            self._observer.schedule(_Handler(), self._dir, recursive=False)
            self._observer.start()
            log.info("conf_watcher: слежу за %s", self._dir)
        except Exception as e:                                # noqa: BLE001
            log.warning("conf_watcher не стартовал (%s) — горячая правка YAML "
                        "недоступна, значения подхватятся при рестарте", e)
            self._observer = None

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=3)
            self._observer = None
