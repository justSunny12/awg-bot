"""Размер хранилища гостя на резерве (tmpfs `/run/awg-bot-guest`) — от размера
базы основы: `rsvzone.store_mb` (16 МБ + 3 × база, не меньше 32 и не больше
4096), обёртки `rsvzone.store`/`take` передают его скрипту зоны в GUEST_STORE_MB,
скрипт (`store_size_mb`, `store_mount`) проверяет значение и монтирует tmpfs
этим размером, уже смонтированный tmpfs — перемонтирует (`--store` процесса
открытия до записи базы и `--take` по базе после журнала).

Скрипт гоняется bash'ем по функциям самого скрипта (обвязка `_Zone` из
test_reserve_zone_review, настоящая `store_mount` возвращается как в её
`_store`); `mount`, `mountpoint`, `stat`, `uname` — подставные в PATH, хост не
трогается.

Цена ошибки: tmpfs фиксированных 32 МБ — база основы больше ~30 МБ в хранилище
не влезает при любых потолках памяти, аварийный переезд срывается «нет места»;
мусор в GUEST_STORE_MB, дошедший до `mount`, — хранилище не смонтировано,
переезд не начинается; без перемонтирования уже стоящего хранилища — размер
остаётся тем, что бот угадал по сжатому пакету; перемонтирование без значения —
живое хранилище гостя ужато до 32 МБ посреди обслуживания.
"""
from __future__ import annotations

import pytest

from awgbot.infra import rsvzone
from tests.conftest import fake_cmd
from tests.unit.test_reserve_zone_review import _Zone, _store_mount_src
from tests.unit.test_reserve_zone_two_clones import _conf

pytestmark = pytest.mark.unit

MB = 1024 * 1024


@pytest.fixture()
def zone(tmp_path):
    return _Zone(tmp_path)


def _host(zone: _Zone, *, mounted: str = "", remount_fail: bool = False) -> None:
    """Ядро 6.12 (tmpfs с noswap); mounted — что уже смонтировано в хранилище (tmpfs | ramfs | пусто)."""
    fake_cmd(zone.bin / "uname", 'case "$1" in -r) echo "6.12.47" ;; *) echo Linux ;; esac\n')
    fake_cmd(zone.bin / "mountpoint", f"exit {0 if mounted else 1}\n")
    fake_cmd(zone.bin / "stat", f'echo "{mounted or "apfs"}"\n')
    fail = "*remount*) exit 32 ;;" if remount_fail else ""
    fake_cmd(zone.bin / "mount", f'echo "mount $*" >> {zone.log}\ncase "$*" in {fail} @@) ;; esac\nexit 0\n')


def _store(zone: _Zone, **env: str):
    return zone.run(_store_mount_src() + "\nstore_mount; echo stored", **env)


def _size(zone: _Zone, value: str | None) -> str:
    env = {} if value is None else {"GUEST_STORE_MB": value}
    r = zone.run("store_size_mb", **env)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


# ── размер от базы ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("size, want", [(0, 32), (5 * MB, 32), (8 * MB, 40), (30 * MB, 106), (30 * MB + 1, 109),
                                        (300 * MB, 916), (3000 * MB, 4096)])
def test_the_store_grows_with_the_base_between_the_old_size_and_the_ceiling(size, want):
    assert rsvzone.store_mb(size) == want


def test_the_store_holds_three_copies_of_the_base():
    """bot.db, bot.db.prev (подмена базы снимком rep.bin) и временная копия снимка — разом."""
    assert rsvzone.store_mb(100 * MB) >= 3 * 100 + 16


# ── проверка значения скриптом ──────────────────────────────────────────────

@pytest.mark.parametrize("value, want", [(None, "32"), ("", "32"), ("106", "106"), ("много", "32"), ("12", "32"),
                                         ("-5", "32"), ("106M", "32"), ("0", "32"), ("4096", "4096"),
                                         ("4097", "4096"), ("99999", "4096"), ("1" * 30, "4096")])
def test_garbage_or_a_too_small_value_gives_the_old_size_and_a_huge_one_the_ceiling(zone, value, want):
    assert _size(zone, value) == want, value


def test_the_script_and_the_bot_agree_on_the_bounds(zone):
    assert _size(zone, str(rsvzone.STORE_FLOOR_MB - 1)) == str(rsvzone.STORE_FLOOR_MB)
    assert _size(zone, str(rsvzone.STORE_CEIL_MB + 1)) == str(rsvzone.STORE_CEIL_MB)


# ── монтирование с размером ─────────────────────────────────────────────────

def test_a_new_store_is_a_tmpfs_of_the_given_size(zone):
    _host(zone)
    r = _store(zone, GUEST_STORE_MB="106")
    assert r.returncode == 0 and "stored" in r.stdout, r.stdout + r.stderr
    assert zone.calls("mount ") == [f"mount -t tmpfs -o size=106M,mode=0700,noswap tmpfs {zone.run_dir}"]


@pytest.mark.parametrize("value", [None, "мусор"])
def test_a_new_store_without_a_sane_value_is_the_old_32_mb(zone, value):
    _host(zone)
    r = _store(zone, **({} if value is None else {"GUEST_STORE_MB": value}))
    assert r.returncode == 0, r.stderr
    assert zone.calls("mount ") == [f"mount -t tmpfs -o size=32M,mode=0700,noswap tmpfs {zone.run_dir}"]


def test_a_mounted_tmpfs_is_remounted_with_the_new_size(zone):
    _host(zone, mounted="tmpfs")
    r = _store(zone, GUEST_STORE_MB="316")
    assert r.returncode == 0 and "ВНИМАНИЕ" not in r.stdout, r.stdout + r.stderr
    assert zone.calls("mount ") == [f"mount -o remount,size=316M {zone.run_dir}"]


def test_a_mounted_store_is_left_alone_without_a_value(zone):
    """Без GUEST_STORE_MB (ручной --store, прежняя обёртка) живое хранилище не ужимается до 32 МБ."""
    _host(zone, mounted="tmpfs")
    r = _store(zone)
    assert r.returncode == 0 and "stored" in r.stdout, r.stderr
    assert not zone.calls("mount "), zone.calls("mount ")


def test_a_mounted_ramfs_has_no_size_to_change(zone):
    _host(zone, mounted="ramfs")
    r = _store(zone, GUEST_STORE_MB="316")
    assert r.returncode == 0, r.stderr
    assert not zone.calls("mount "), zone.calls("mount ")


def test_a_refused_remount_is_a_warning_and_the_old_store_stays(zone):
    _host(zone, mounted="tmpfs", remount_fail=True)
    r = _store(zone, GUEST_STORE_MB="40")
    assert r.returncode == 0 and "stored" in r.stdout, r.stderr
    assert "ВНИМАНИЕ: хранилище гостя не перемонтировано размером 40M" in r.stdout, r.stdout


def test_take_remounts_the_store_with_the_size_from_the_open_process(zone):
    """--take: хранилище уже стоит (бот смонтировал его по пакету) — размер по базе после журнала."""
    _host(zone, mounted="tmpfs")
    (zone.run_dir / "awg").mkdir(parents=True, exist_ok=True)
    r = zone.run(_store_mount_src() + "\ncmd_take", stdin=_conf(45877, 1), GUEST_IF="awg0", GUEST_ADDR="10.8.1.1/24",
                 GUEST_STORE_MB="136")
    assert r.returncode == 0 and "take: ok" in r.stdout, r.stdout + r.stderr
    assert zone.calls("mount ") == [f"mount -o remount,size=136M {zone.run_dir}"]


# ── передача скрипту ────────────────────────────────────────────────────────

def test_store_and_take_pass_the_size_to_the_script(monkeypatch):
    seen = []
    monkeypatch.setattr(rsvzone, "run", lambda mode, env=None, stdin=None, **kw: seen.append((mode, env or {})) or "")
    rsvzone.store(106)
    rsvzone.store()
    rsvzone.take("[Interface]\n", iface="awg0", addr="10.8.1.1/24", dns=[], upstreams=[], admin=[], store_size_mb=136)
    rsvzone.take("[Interface]\n", iface="awg0", addr="10.8.1.1/24", dns=[], upstreams=[], admin=[])
    assert seen[0] == ("store", {"GUEST_STORE_MB": "106"}), seen[0]
    assert seen[1] == ("store", {}), "без размера — смонтированное не трогается"
    assert seen[2][0] == "take" and seen[2][1]["GUEST_STORE_MB"] == "136"
    assert "GUEST_STORE_MB" not in seen[3][1], "без значения — умолчание скрипта"
