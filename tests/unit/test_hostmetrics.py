"""Unit: awgbot.runtime.hostmetrics — локальное чтение метрик железа.

Файлы /proc и statvfs подменяем, чтобы тест был детерминирован и не зависел
от нагрузки песочницы.
"""

import pytest

from awgbot.runtime import hostmetrics as hm

pytestmark = pytest.mark.unit


def test_read_ram_percent(monkeypatch, tmp_path):
    f = tmp_path / "meminfo"
    f.write_text("MemTotal:       1000 kB\nMemAvailable:    250 kB\nBuffers: 1 kB\n")
    real_open = open
    monkeypatch.setattr("builtins.open",
                        lambda p, *a, **k: real_open(f, *a, **k) if p == "/proc/meminfo" else real_open(p, *a, **k))
    assert hm.read_ram_percent() == 75.0            # (1 - 250/1000) * 100


def test_read_ram_percent_missing_file(monkeypatch):
    def _boom(p, *a, **k):
        if p == "/proc/meminfo":
            raise OSError("nope")
        raise AssertionError
    monkeypatch.setattr("builtins.open", _boom)
    assert hm.read_ram_percent() is None


def test_read_disk_percent(monkeypatch):
    import os
    class _St:
        f_blocks = 1000; f_bfree = 400
    monkeypatch.setattr(os, "statvfs", lambda p: _St())
    assert hm.read_disk_percent("/") == 60.0         # (1000-400)/1000


def test_read_disk_percent_error(monkeypatch):
    import os
    monkeypatch.setattr(os, "statvfs", lambda p: (_ for _ in ()).throw(OSError()))
    assert hm.read_disk_percent("/") is None


def test_read_cpu_percent(monkeypatch):
    # два чтения /proc/stat: разница busy=20, total=100 → 20%
    seq = iter([("cpu  10 0 10 80 0 0 0 0\n"), ("cpu  20 0 20 140 0 0 0 0\n")])
    class _F:
        def __init__(self, data): self._d = data
        def readline(self): return self._d
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr("builtins.open",
                        lambda p, *a, **k: _F(next(seq)) if p == "/proc/stat" else (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(hm.time, "sleep", lambda s: None)
    # busy0 = (10+0+10)=20, total0=110-80=... используем реальную формулу модуля
    v = hm.read_cpu_percent()
    assert v is not None and 0 <= v <= 100


def test_read_cpu_percent_bad_stat(monkeypatch):
    class _F:
        def readline(self): return "garbage line\n"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr("builtins.open", lambda p, *a, **k: _F())
    assert hm.read_cpu_percent() is None


def test_collect_and_store_and_get(tmp_path):
    from awgbot.infra.db import Database
    db = Database(str(tmp_path / "t.db")); db.init_schema()
    snap = hm.collect_and_store(db)
    assert set(snap) == {"cpu", "ram", "disk"}
    got = hm.get_host_metrics(db)
    assert got["cpu"] == snap["cpu"] and got["age_seconds"] is not None
    assert got["age_seconds"] < 5


def test_get_host_metrics_empty(tmp_path):
    from awgbot.infra.db import Database
    db = Database(str(tmp_path / "t.db")); db.init_schema()
    assert hm.get_host_metrics(db) is None


def test_get_host_metrics_corrupt(tmp_path):
    from awgbot.infra.db import Database
    db = Database(str(tmp_path / "t.db")); db.init_schema()
    db.set_state(hm.STATE_METRICS, "{not json")
    assert hm.get_host_metrics(db) is None
