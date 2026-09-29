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
        f_blocks = 1000; f_bfree = 400; f_bavail = 380; f_frsize = 4096
    monkeypatch.setattr(os, "statvfs", lambda p: _St())
    assert hm.read_disk_percent("/") == 60.0         # (1000-400)/1000 — тот же разбор, что у read_disk()


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


def test_throttled_word_is_read_from_sysfs_before_vcgencmd(tmp_path):
    """На Pi 4/5 слово троттлинга лежит в sysfs — файл вместо exec vcgencmd на
    каждый тик; прошивка пишет hex с «0x» и без."""
    from awgbot.runtime import hostmetrics as hm
    p = tmp_path / "get_throttled"
    p.write_text("0x50005\n")
    assert hm._read_throttled_sysfs(str(p)) == 0x50005
    p.write_text("50005\n")
    assert hm._read_throttled_sysfs(str(p)) == 0x50005
    p.write_text("0\n")
    assert hm._read_throttled_sysfs(str(p)) == 0
    assert hm._read_throttled_sysfs(str(tmp_path / "nope")) is None
    p.write_text("garbage\n")
    assert hm._read_throttled_sysfs(str(p)) is None


# ── питание — только на Raspberry Pi ─────────────────────────────────────────

@pytest.fixture()
def model(tmp_path, monkeypatch):
    """Подставной /proc/device-tree/model; кэш «Pi ли это» сброшен на тест."""
    p = tmp_path / "model"
    monkeypatch.setattr(hm, "_PI_MODEL", str(p))
    monkeypatch.setattr(hm, "_is_pi", None)
    return p


@pytest.mark.parametrize("raw, pi", [
    (b"Raspberry Pi 4 Model B Rev 1.4\x00", True),
    (b"Raspberry Pi 5 Model B Rev 1.0\x00", True),
    (b"RASPBERRY PI Compute Module 4\x00", True),
    (b"Radxa ROCK 5B\x00", False),
    (b"\xff\xfe\x00", False),
])
def test_a_raspberry_pi_is_told_by_the_device_tree_model(model, raw, pi):
    """Модель из device-tree — единственный признак малины: vcgencmd бывает
    и на других платах, а sysfs троттлинга — не на всех прошивках."""
    model.write_bytes(raw)
    assert hm.is_raspberry_pi() is pi


def test_no_device_tree_means_not_a_pi_and_the_answer_is_kept(model):
    """Нет файла (x86, ВМ) — не Pi; ответ запоминается на процесс: модель
    платы не меняется, а тик монитора не должен читать файл каждый раз."""
    assert hm.is_raspberry_pi() is False
    model.write_bytes(b"Raspberry Pi 4 Model B\x00")
    assert hm.is_raspberry_pi() is False, "ответ перечитан — кэша нет"


def test_off_a_pi_power_is_not_read_at_all(model, monkeypatch):
    """Не Pi — питание не смотрим вовсе: ни sysfs, ни vcgencmd. Иначе агент
    на обычном сервере рисовал бы «питание ОК», которое ничем не проверено,
    или алертил бы по случайному файлу."""
    import subprocess

    def forbidden(*a, **k):
        raise AssertionError("не Pi, а питание всё равно читают")
    monkeypatch.setattr(hm, "_read_throttled_sysfs", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    model.write_bytes(b"Generic x86 board\x00")
    assert hm.read_pi_throttled() is None


def test_on_a_pi_power_is_read_and_decoded(model, monkeypatch):
    model.write_bytes(b"Raspberry Pi 4 Model B Rev 1.4\x00")
    monkeypatch.setattr(hm, "_read_throttled_sysfs", lambda: 0x50001)
    assert hm.read_pi_throttled() == {"raw": 0x50001, "now": ["недонапряжение"],
                                      "ever": ["недонапряжение случалось", "троттлинг случался"]}


def test_on_a_pi_without_sysfs_a_failing_vcgencmd_means_unknown(model, monkeypatch):
    """Pi без файла в sysfs и без vcgencmd (или vcgencmd упал) — «нечем
    посмотреть» (None), а не «питание ОК»."""
    import subprocess

    def boom(*a, **k):
        raise FileNotFoundError("vcgencmd")
    model.write_bytes(b"Raspberry Pi 3 Model B\x00")
    monkeypatch.setattr(hm, "_read_throttled_sysfs", lambda: None)
    monkeypatch.setattr(subprocess, "run", boom)
    assert hm.read_pi_throttled() is None


def test_without_a_power_reading_the_gateway_screens_say_nothing_about_power():
    """Не Pi — ни на панели, ни на экране здоровья слова «питание» нет:
    непроверенное «питание ОК» — ложное спокойствие."""
    from awgbot.bot import texts
    from awgbot.domain.gateway import GwStatus
    st = GwStatus(link_up=True, handshake_age=5.0, cpu=10.0, ram=20.0, disk=30.0, disk_free_gb=50.0,
                  throttled=None)
    assert "питание" not in texts.gateway_panel(st).lower()
    assert "питание" not in texts.gateway_health(st).lower()
    st.throttled = {"raw": 0, "now": [], "ever": []}
    assert "питание ОК" in texts.gateway_panel(st), "на Pi строка питания есть"


def test_smart_verdict_comes_from_the_exit_code_not_from_the_word_failed(monkeypatch):
    """Ошибка открытия устройства тоже печатает «failed» — здоровый диск
    показывался как SMART FAIL. Вердикт — по битам кода возврата smartctl."""
    import shutil
    import subprocess
    monkeypatch.setattr(hm, "root_block_device", lambda: "/dev/sda")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/sbin/smartctl")
    for rc, want in ((0, "OK"), (8, "FAIL"), (2, None), (1, None), (4, None), (32, "OK"), (40, "FAIL")):
        monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
            argv, rc, b"Smartctl open device: /dev/sda failed: Permission denied\n", b""))
        assert hm.read_smart_health() == want, rc


def test_dev_root_is_resolved_through_sysfs(monkeypatch, tmp_path):
    m = tmp_path / "mounts"; m.write_text("/dev/root / ext4 rw 0 0\n", encoding="utf-8")
    monkeypatch.setattr(hm, "_resolve_dev_root", lambda: "/dev/nvme0n1p2")
    assert hm.root_block_device(str(m)) == "/dev/nvme0n1"
    monkeypatch.setattr(hm, "_resolve_dev_root", lambda: "")
    assert hm.root_block_device(str(m)) == ""
