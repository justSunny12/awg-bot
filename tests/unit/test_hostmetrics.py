"""Unit: awgbot.infra.hostmetrics — локальное чтение метрик железа.

Файлы /proc и statvfs подменяем, чтобы тест был детерминирован и не зависел
от нагрузки песочницы.
"""

import pytest

from awgbot.infra import hostmetrics as hm

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
    """Загрузка CPU — дельта двух чтений /proc/stat. База прошлого замера —
    глобальная на процесс: на Linux её уже заполнил настоящий /proc/stat
    (test_collect_and_store_and_get в том же воркере), и дельта к подставным
    числам выходила отрицательной — None. Сбрасываем: первый вызов после старта."""
    monkeypatch.setattr(hm, "_last_cpu_stat", None)
    # busy = всё, кроме idle+iowait: 20/100 → 40/180, дельта 20 из 80 → 25%
    seq = iter([("cpu  10 0 10 80 0 0 0 0\n"), ("cpu  20 0 20 140 0 0 0 0\n")])
    class _F:
        def __init__(self, data): self._d = data
        def readline(self): return self._d
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr("builtins.open",
                        lambda p, *a, **k: _F(next(seq)) if p == "/proc/stat" else (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(hm.time, "sleep", lambda s: None)
    assert hm.read_cpu_percent() == 25.0, "загрузка — доля busy в дельте total между двумя чтениями"
    # второй вызов — к прошлому чтению, без новой паузы: 40/180 → 60/260 = 25%
    seq = iter([("cpu  30 0 30 200 0 0 0 0\n")])
    assert hm.read_cpu_percent() == 25.0, "второй замер считается от прошлого чтения, а не от свежей пробы"


def test_read_cpu_percent_bad_stat(monkeypatch):
    monkeypatch.setattr(hm, "_last_cpu_stat", None)
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
    # снимок несёт и «свободно МБ», и список носителей — их читают панели
    assert set(snap) == {"cpu", "ram", "disk", "ram_free_mb", "ram_used_mb", "ram_total_mb", "disks"}, snap
    assert isinstance(snap["disks"], list)
    assert all(len(d) == 3 for d in snap["disks"]), "элемент — [занято ГБ, всего ГБ, точка]"
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
    from awgbot.infra import hostmetrics as hm
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
    """Модель из device-tree — единственный признак шлюза: vcgencmd бывает
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


# ── носители по /proc/mounts ─────────────────────────────────────────────────

class _Vfs:
    """Подставной statvfs: объём по точке монтирования, блоки по 4096."""
    def __init__(self, blocks: int, bfree: int):
        self.f_blocks, self.f_bfree, self.f_bavail, self.f_frsize = blocks, bfree, bfree, 4096


_G = 1024 ** 3 // 4096      # блоков в гигабайте при f_frsize = 4096


@pytest.fixture()
def mounts(tmp_path, monkeypatch):
    """Файл mounts как на шлюзе под OMV: корень, bind того же устройства,
    загрузочный vfat, overlay контейнера, tmpfs, два настоящих диска
    (второй перечислен раньше по файлу, но позже по точке), snap."""
    import os
    m = tmp_path / "mounts"
    m.write_text("\n".join([
        "/dev/mmcblk0p2 / ext4 rw,noatime 0 0",
        "/dev/mmcblk0p2 /var/lib/docker/containers ext4 rw,noatime 0 0",   # bind того же устройства, и под docker
        "/dev/mmcblk0p2 /home/mirror ext4 rw 0 0",                         # bind того же устройства — не второй диск
        "/dev/mmcblk0p1 /boot/firmware vfat rw 0 0",
        "overlay /var/lib/docker/overlay2/abc/merged overlay rw 0 0",
        "tmpfs /run tmpfs rw 0 0",
        "/dev/sdb1 /srv/dev-disk-by-uuid-bbbb ext4 rw 0 0",
        "/dev/sda1 /srv/dev-disk-by-uuid-aaaa xfs rw 0 0",
        "/dev/loop3 /snap/core/1 squashfs ro 0 0",
        "/dev/sdc1 /mnt/usb exfat rw 0 0",
        "",
    ]), encoding="utf-8")
    sizes = {"/": _Vfs(200 * _G, 190 * _G),                      # 200 ГБ, занято 10
             "/var/lib/docker/containers": _Vfs(200 * _G, 190 * _G),
             "/home/mirror": _Vfs(200 * _G, 190 * _G),
             "/boot/firmware": _Vfs(_G // 2, _G // 4),
             "/srv/dev-disk-by-uuid-bbbb": _Vfs(1720 * _G + _G // 3, 1720 * _G + _G // 3 - 100 * _G),   # 1720,3 ГБ, занято 100
             "/srv/dev-disk-by-uuid-aaaa": _Vfs(931 * _G + _G // 2, 931 * _G),                           # 931,5 ГБ, занято 0,5
             "/mnt/usb": _Vfs(0, 0)}                               # пустой f_blocks — ещё не прочитался
    monkeypatch.setattr(os, "statvfs", lambda p: sizes[p])
    return m


def test_read_disks_lists_real_devices_once_root_first_then_by_mount_point(mounts):
    """Панель показывает носители: bind одного устройства — один раз (иначе
    у админа «три диска» по 200 ГБ на одной SD-карте), /boot, overlay,
    tmpfs и snap — не носители; корень первым, дальше по точке монтирования,
    а не по порядку в файле; занято — f_blocks − f_bfree, как у df."""
    assert hm.read_disks(str(mounts)) == [
        {"mount": "/", "used_gb": 10.0, "total_gb": 200.0},
        {"mount": "/srv/dev-disk-by-uuid-aaaa", "used_gb": 0.5, "total_gb": 931.5},
        {"mount": "/srv/dev-disk-by-uuid-bbbb", "used_gb": 100.0, "total_gb": 1720.3},
    ]


def test_read_disks_skips_a_mount_statvfs_cannot_read(mounts, monkeypatch):
    """Носитель отвалился между чтением mounts и statvfs — пропускаем его,
    остальные остаются: тик монитора не должен падать из-за одного диска."""
    import os
    real = os.statvfs

    def boom(p):
        if p == "/srv/dev-disk-by-uuid-aaaa":
            raise OSError("Input/output error")
        return real(p)
    monkeypatch.setattr(os, "statvfs", boom)
    assert [d["mount"] for d in hm.read_disks(str(mounts))] == ["/", "/srv/dev-disk-by-uuid-bbbb"]


def test_read_disks_without_mounts_file_is_empty(tmp_path):
    assert hm.read_disks(str(tmp_path / "nope")) == []


def test_read_disks_ignores_short_and_foreign_lines(tmp_path, monkeypatch):
    """Строка без ФС, не-/dev источник (NFS, cifs) и устройство с неизвестной
    ФС (squashfs) — не носители; остаётся только настоящий корень."""
    import os
    m = tmp_path / "mounts"
    m.write_text("garbage\n"
                 "nas:/export /mnt/nas nfs4 rw 0 0\n"
                 "//srv/share /mnt/smb cifs rw 0 0\n"
                 "/dev/loop0 /snap/x/1 squashfs ro 0 0\n"
                 "/dev/sda2 / ext4 rw 0 0\n", encoding="utf-8")
    monkeypatch.setattr(os, "statvfs", lambda p: _Vfs(10 * _G, 5 * _G))
    assert hm.read_disks(str(m)) == [{"mount": "/", "used_gb": 5.0, "total_gb": 10.0}]


def test_collect_carries_ram_used_total_and_disks_from_the_readers(monkeypatch):
    """Снимок хоста для панели сервера: «занято/всего МБ» — из read_ram_mb,
    «свободно МБ» — из read_ram, носители — списками [занято, всего, точка]
    (JSON в БД и канал)."""
    monkeypatch.setattr(hm, "read_cpu_percent", lambda: 7.0)
    monkeypatch.setattr(hm, "read_ram", lambda: (17.0, 6436))
    monkeypatch.setattr(hm, "read_ram_mb", lambda: (1340, 7812))
    monkeypatch.setattr(hm, "read_disk_percent", lambda path="/": 5.0)
    monkeypatch.setattr(hm, "read_disks", lambda mounts="/proc/mounts": [
        {"mount": "/", "used_gb": 10.0, "total_gb": 200.0}])
    assert hm.collect() == {"cpu": 7.0, "ram": 17.0, "ram_free_mb": 6436, "ram_used_mb": 1340,
                            "ram_total_mb": 7812, "disk": 5.0, "disks": [[10.0, 200.0, "/"]]}
    monkeypatch.setattr(hm, "read_ram", lambda: None)
    monkeypatch.setattr(hm, "read_ram_mb", lambda: None)
    snap = hm.collect()
    assert (snap["ram"], snap["ram_free_mb"], snap["ram_used_mb"], snap["ram_total_mb"]) == (None,) * 4, \
        "meminfo не прочитался — все поля памяти None, не падение"


# ── read_ram_mb: занято/всего по /proc/meminfo ───────────────────────────────

@pytest.fixture()
def meminfo(tmp_path, monkeypatch):
    """Подставной /proc/meminfo: тест пишет в файл, open() отдаёт его."""
    f = tmp_path / "meminfo"
    real_open = open
    monkeypatch.setattr("builtins.open",
                        lambda p, *a, **k: real_open(f, *a, **k) if p == "/proc/meminfo" else real_open(p, *a, **k))
    return f


def test_read_ram_mb_is_used_and_total_in_whole_megabytes(meminfo):
    """Занято — MemTotal − MemAvailable (кэш и буферы — не «занято», как у free),
    оба — целыми МБ с округлением вниз; порядок строк в файле не важен."""
    meminfo.write_text("MemFree:          10 kB\nMemAvailable:  6590464 kB\nMemTotal:       7999488 kB\n")
    assert hm.read_ram_mb() == (1376, 7812), "(7999488−6590464)//1024, 7999488//1024"
    # остаток меньше мегабайта отбрасывается, а не округляется вверх
    meminfo.write_text("MemTotal:  1048575 kB\nMemAvailable:  1048 kB\n")
    assert hm.read_ram_mb() == (1022, 1023), "1047527//1024 и 1048575//1024 — вниз, а не 1023/1024"


@pytest.mark.parametrize("body", [
    "MemFree: 10 kB\nBuffers: 1 kB\n",                  # нет ни MemTotal, ни MemAvailable
    "MemTotal: 1000 kB\nMemFree: 10 kB\n",              # старое ядро без MemAvailable
    "MemAvailable: 100 kB\n",                             # нет MemTotal
    "MemTotal: 0 kB\nMemAvailable: 0 kB\n",             # нулевое всего — делить нечего
    "",
])
def test_read_ram_mb_without_both_fields_is_none(meminfo, body):
    """Нет пары MemTotal/MemAvailable — None, и панель рисует процент, а не
    «занято 0 МБ/всего 0 МБ»."""
    meminfo.write_text(body)
    assert hm.read_ram_mb() is None


def test_read_ram_mb_missing_file_is_none(monkeypatch):
    def _boom(p, *a, **k):
        if p == "/proc/meminfo":
            raise OSError("nope")
        raise AssertionError
    monkeypatch.setattr("builtins.open", _boom)
    assert hm.read_ram_mb() is None
