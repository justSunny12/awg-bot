"""
hostmetrics.py — локальные метрики железа co-located хоста (CPU/RAM/диск).

Бот живёт на том же хосте, что и awg-контейнер, поэтому метрики читаем напрямую
из /proc и statvfs — без агента, сети, подписи и «возраста стука». Источник
всегда свежий: снимок обновляет монитор планировщика каждый свой тик.

Формат хранения в server_state (ключ host_metrics) совместим с прежним:
{"cpu": float|None, "ram": float|None, "disk": float|None, "ts": iso} — инфобокс
и ресурс-алерты (services.check_resource_alerts) работают без изменений.
"""
from __future__ import annotations

import json
import logging
import os
import time

from awgbot.util import timeutil

log = logging.getLogger("awgbot.hostmetrics")

STATE_METRICS = "host_metrics"                # JSON последнего снимка метрик

_CPU_SAMPLE_SECONDS = 0.25                    # окно замера CPU (двойное чтение /proc/stat)


def _read_proc_stat() -> tuple[int, int] | None:
    """(busy, total) джиффи из агрегатной строки cpu в /proc/stat."""
    try:
        with open("/proc/stat", encoding="ascii") as f:
            line = f.readline()
    except OSError:
        return None
    parts = line.split()
    if not parts or parts[0] != "cpu" or len(parts) < 5:
        return None
    vals = [int(x) for x in parts[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)     # idle + iowait
    total = sum(vals)
    return total - idle, total


def read_cpu_percent() -> float | None:
    """Загрузка CPU, %: дельта busy/total между двумя чтениями /proc/stat.
    Блокирует поток на _CPU_SAMPLE_SECONDS — вызывать через asyncio.to_thread."""
    a = _read_proc_stat()
    if a is None:
        return None
    time.sleep(_CPU_SAMPLE_SECONDS)
    b = _read_proc_stat()
    if b is None:
        return None
    dbusy, dtotal = b[0] - a[0], b[1] - a[1]
    if dtotal <= 0:
        return None
    return round(100.0 * dbusy / dtotal, 1)


def read_ram_percent() -> float | None:
    """Занятость RAM, %: 100 × (1 − MemAvailable/MemTotal) из /proc/meminfo."""
    total = avail = None
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1])
                if total is not None and avail is not None:
                    break
    except OSError:
        return None
    if not total or avail is None:
        return None
    return round(100.0 * (1 - avail / total), 1)


def read_disk_percent(path: str = "/") -> float | None:
    """Занятость диска, % (корневая ФС): statvfs, доля занятых блоков."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    if st.f_blocks <= 0:
        return None
    used = st.f_blocks - st.f_bfree
    return round(100.0 * used / st.f_blocks, 1)


def collect() -> dict:
    """Снимок {cpu, ram, disk} (float % или None по каждому ресурсу).
    Блокирующий (CPU-замер) — через asyncio.to_thread."""
    return {"cpu": read_cpu_percent(),
            "ram": read_ram_percent(),
            "disk": read_disk_percent()}


def collect_and_store(db) -> dict:
    """Снять метрики локально и записать снимок в state. Возвращает снимок
    (для гистерезиса ресурс-алертов)."""
    m = collect()
    db.set_state(STATE_METRICS, json.dumps(
        {**m, "ts": timeutil.to_iso(timeutil.now())}))
    return m


def get_host_metrics(db) -> dict | None:
    """Последний снимок {cpu,ram,disk,ts,age_seconds} или None."""
    raw = db.get_state(STATE_METRICS)
    if not raw:
        return None
    try:
        snap = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    ts = snap.get("ts")
    snap["age_seconds"] = ((timeutil.now() - timeutil.parse_iso(ts)).total_seconds()
                           if ts else None)
    return snap


__all__ = ["collect", "collect_and_store", "get_host_metrics", "STATE_METRICS",
           "read_cpu_percent", "read_ram_percent", "read_disk_percent"]
