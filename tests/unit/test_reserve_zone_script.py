"""Скрипт гостевой зоны резерва install/reserve-zone-setup.sh: режимы этапа —
`--plan` (что умеет хост, без root) и `--selftest` (гипотезы на живом
модуле, только root); остальные режимы пока отказывают.

Скрипт гоняется целиком bash'ем; `uname`, `modinfo`, `ss`, `id` подменены
в PATH, так что вывод не зависит от машины, где идёт прогон.

Цена ошибки: `--plan`, который не печатает строку `key=value` или печатает
не то, — бот решает о хранилище в памяти или порте вебхука по мусору
(ramfs вместо tmpfs без свопа, занятый порт 443 вместо свободного);
`--selftest` без root, который начинает создавать пространства, — частично
собранная зона на хосте; режим будущего этапа, который «успешно» ничего не
делает, — бот думает, что зона поднята.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import fake_cmd

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "install" / "reserve-zone-setup.sh"
KEYS = ("store", "cake", "netns", "awg", "webhook_port", "zone")


class _Box:
    def __init__(self, tmp_path: Path):
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.kernel("6.12.47+rpt-rpi-v8")
        self.mods(cake=True, awg=True)
        self.busy()
        fake_cmd(self.bin / "id", 'case "$1" in -u) echo 1000 ;; *) echo "uid=1000" ;; esac\n')

    def kernel(self, release: str) -> None:
        fake_cmd(self.bin / "uname", f'case "$1" in -r) echo "{release}" ;; *) echo Linux ;; esac\n')

    def mods(self, *, cake: bool, awg: bool) -> None:
        have = " ".join(n for n, on in (("sch_cake", cake), ("amneziawg", awg)) if on)
        fake_cmd(self.bin / "modinfo", f'for m in {have}; do [ "$1" = "$m" ] && exit 0; done; exit 1\n')

    def busy(self, *ports: int) -> None:
        lines = "".join(f'case "$*" in *":{p}"*) echo "LISTEN 0 4096 0.0.0.0:{p} 0.0.0.0:*" ;; esac\n' for p in ports)
        fake_cmd(self.bin / "ss", lines + "exit 0\n")

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=30,
                              env={"PATH": f"{self.bin}:/usr/bin:/bin", "RSV_ZONE": "awgtst-нет-такой"})


@pytest.fixture()
def box(tmp_path):
    return _Box(tmp_path)


def _plan(r) -> dict:
    assert r.returncode == 0, r.stdout + r.stderr
    out = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    assert tuple(out) == KEYS, f"строки плана: {r.stdout!r}"
    return out


def test_the_plan_prints_every_line_without_root(box):
    p = _plan(box.run("--plan"))
    assert p["netns"] in ("yes", "no"), "netns смотрит на /proc хоста прогона"
    assert {k: v for k, v in p.items() if k != "netns"} == {"store": "tmpfs-noswap", "cake": "yes", "awg": "yes",
                                                            "webhook_port": "443", "zone": "absent"}


@pytest.mark.parametrize("release, store", [("6.12.47+rpt-rpi-v8", "tmpfs-noswap"), ("6.4.0-1-amd64", "tmpfs-noswap"),
                                            ("7.0.1", "tmpfs-noswap"), ("6.3.13", "ramfs"),
                                            ("6.1.0-26-amd64", "ramfs"), ("5.15.0", "ramfs")])
def test_memory_store_without_swap_needs_kernel_6_4(box, release, store):
    """tmpfs с noswap — с ядра 6.4; ниже только ramfs: ключи гостя в своп не
    попадают ни там, ни там, но tmpfs без noswap — попадают."""
    box.kernel(release)
    assert _plan(box.run("--plan"))["store"] == store


def _loaded(name: str) -> bool:
    """Модуль уже загружен на машине прогона: скрипт видит его и через
    /proc/modules, подмена modinfo этого не скрывает."""
    try:
        return any(line.split()[0] == name for line in Path("/proc/modules").read_text().splitlines())
    except OSError:
        return False


def test_missing_modules_are_reported_as_no(box):
    box.mods(cake=False, awg=False)
    p = _plan(box.run("--plan"))
    want = tuple("yes" if _loaded(m) else "no" for m in ("sch_cake", "amneziawg"))
    assert (p["cake"], p["awg"]) == want


@pytest.mark.parametrize("busy, port", [((443,), "80"), ((443, 80), "88"), ((443, 80, 88), "8443"),
                                        ((443, 80, 88, 8443), "none"), ((8443,), "443")])
def test_the_webhook_port_is_the_first_free_one_telegram_accepts(box, busy, port):
    box.busy(*busy)
    assert _plan(box.run("--plan"))["webhook_port"] == port


def test_the_selftest_refuses_without_root_before_touching_anything(box, tmp_path):
    calls = tmp_path / "ip.log"
    fake_cmd(box.bin / "ip", f'echo "ip $*" >> "{calls}"\n')
    r = box.run("--selftest")
    assert r.returncode == 1 and "root" in r.stderr
    assert not calls.exists(), "без root скрипт успел позвать ip"


@pytest.mark.parametrize("mode", ["--take", "--serve", "--shell", "--restart-clone", "--relay", "--teardown",
                                  "--wipe", "--status"])
def test_modes_of_later_stages_refuse(box, mode):
    r = box.run(mode)
    assert r.returncode == 1, f"{mode}: код {r.returncode}"
    assert "следующий этап" in r.stderr


@pytest.mark.parametrize("args", [(), ("--nope",), ("plan",)])
def test_an_unknown_mode_is_a_usage_error(box, args):
    r = box.run(*args)
    assert r.returncode == 2 and "--plan" in r.stdout
