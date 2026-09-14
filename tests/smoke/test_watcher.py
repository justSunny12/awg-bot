"""Smoke: вотчдог импортируется и предъявляет ожидаемый публичный интерфейс.

Реальный inotify-цикл не поднимаем (нужен живой контейнер/пути Amnezia) — только
проверяем контракт, на который опирается scheduler.job_monitor (alive/
ensure_watching) и что конструктор не тянет побочных эффектов при импорте.
"""
import pytest

pytestmark = pytest.mark.smoke


def test_watcher_construct_does_not_start_thread():
    from awgbot.runtime.watcher import AwgWatcher
    w = AwgWatcher(on_change=lambda: None)
    # до ensure_watching() наблюдение не запущено
    assert w.alive() is False
