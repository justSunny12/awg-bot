"""Скрипт гостевой зоны резерва install/reserve-zone-setup.sh: `--plan` (что
умеет хост, без root), `--status` (состояние строками, без root), `--selftest`
и остальные режимы (оболочка, хранилище, переезд, обслуживание, возврат,
снятие, стирание) — только root; без него отказывают, ничего не тронув. Текст
юнита бота основы на резерве — песочница хранения (§3.3.1) — проверяется
прогоном функции из скрипта.

Скрипт гоняется целиком bash'ем; `uname`, `modinfo`, `ss`, `id` подменены
в PATH, так что вывод не зависит от машины, где идёт прогон.

Цена ошибки: `--plan`, который не печатает строку `key=value` или печатает
не то, — бот решает о хранилище в памяти или порте вебхука по мусору
(ramfs вместо tmpfs без свопа, занятый порт 443 вместо свободного);
`--selftest` без root, который начинает создавать пространства, — частично
собранная зона на хосте; режим, который без root «успешно» ничего не делает,
— бот думает, что зона поднята; юнит гостя без `TemporaryFileSystem` над
`/etc/awg-bot` и `/var/lib/awg-bot` — бот основы на резерве читает env и базу
самого резерва; без `LogNamespace` — журнал гостя оседает на диск резерва;
без `MemorySwapMax=0` и `LimitCORE=0` — ключи основы уходят в своп и в дамп.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import fake_cmd

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "install" / "reserve-zone-setup.sh"
KEYS = ("store", "cake", "netns", "awg", "webhook_port", "zone", "dnsmasq")


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

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        base = self.bin.parent
        full = {"PATH": f"{self.bin}:/usr/bin:/bin", "RSV_ZONE": "awgtst-нет-такой",
                "RSV_RUN": str(base / "run"), "RSV_VAR": str(base / "var"), "RSV_STATE_DIR": str(base / "state")}
        full.update(env)
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=30, env=full)


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
    assert p["dnsmasq"] in ("yes", "no"), "dnsmasq смотрит на /usr/sbin хоста прогона"
    assert {k: v for k, v in p.items() if k not in ("netns", "dnsmasq")} == {"store": "tmpfs-noswap", "cake": "yes", "awg": "yes",
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
    """Модуль amneziawg уже загружен на машине прогона: его скрипт видит ещё
    и через /proc/modules, подмена modinfo этого не скрывает (sch_cake —
    только по modinfo)."""
    try:
        return any(line.split()[0] == name for line in Path("/proc/modules").read_text().splitlines())
    except OSError:
        return False


def test_missing_modules_are_reported_as_no(box):
    box.mods(cake=False, awg=False)
    p = _plan(box.run("--plan"))
    assert p["cake"] == "no", "без модуля по modinfo план обещает CAKE"
    assert p["awg"] == ("yes" if _loaded("amneziawg") else "no")


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


@pytest.mark.parametrize("mode", ["--shell", "--store", "--take", "--serve", "--restart-clone", "--relay",
                                  "--teardown", "--wipe"])
def test_modes_that_change_the_host_refuse_without_root_and_touch_nothing(box, tmp_path, mode):
    """Без root ни одна из команд зоны не доходит до ip, mount, systemctl и nft."""
    calls = tmp_path / "calls.log"
    for tool in ("ip", "mount", "umount", "systemctl", "nft", "tc", "awg"):
        fake_cmd(box.bin / tool, f'echo "{tool} $*" >> "{calls}"\n')
    r = box.run(mode)
    assert r.returncode == 1, f"{mode}: код {r.returncode}"
    assert "root" in r.stderr
    assert not calls.exists(), f"{mode}: без root успели позвать {calls.read_text()}"
    assert not (tmp_path / "run").exists() and not (tmp_path / "var").exists(), "созданы каталоги хранилища"


def test_status_prints_key_value_lines_without_root_on_a_bare_host(box):
    r = box.run("--status")
    assert r.returncode == 0, r.stderr
    got = dict(line.split("=", 1) for line in r.stdout.splitlines())
    assert got == {"zone": "absent", "phase": "", "iface": "", "ifaces": "", "store": "absent", "guest": "inactive",
                   "rep": "absent"}


def test_status_reports_the_state_file_and_the_encrypted_snapshot(box, tmp_path):
    """Бот решает по --status, что зона поднята и реплика на месте; значения
    берутся из state.json (его пишет сам скрипт) и из файла rep.bin."""
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.json").write_text('{"phase": "serving", "iface": "awg0"}')
    (tmp_path / "var").mkdir()
    (tmp_path / "var" / "rep.bin").write_bytes(b"DUMMY")
    got = dict(line.split("=", 1) for line in box.run("--status").stdout.splitlines())
    assert got["phase"] == "serving" and got["iface"] == "awg0" and got["rep"] == "present"


def _unit_text(tmp_path: Path) -> str:
    """Текст юнита гостя: функция write_units из скрипта, пишущая в подставные
    пути (systemctl подменён)."""
    src = SCRIPT.read_text()
    start = src.index("write_units() {")
    end = src.index("\n}\n", start) + 3
    bin_ = tmp_path / "ubin"
    bin_.mkdir()
    fake_cmd(bin_ / "systemctl", "exit 0\n")
    unit, dns = tmp_path / "guest.service", tmp_path / "dns.service"
    prog = (f'ZONE=awg-guest; NS=/run/netns/awg-guest; RUN=/run/awg-bot-guest; VAR=/var/lib/awg-bot-guest; '
            f'STATE_DIR=/run/awg-bot/rsv; INSTALL_DIR=/opt/awg-bot; UNIT="{unit}"; DNS_UNIT="{dns}"\n'
            f'{src[start:end]}\nwrite_units awg0\n')
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{bin_}:/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    return unit.read_text()


def test_the_guest_unit_hides_the_hosts_secrets_and_keeps_keys_out_of_swap_dumps_and_disk_journal(tmp_path):
    text = _unit_text(tmp_path)
    lines = {ln.split("=", 1)[0]: ln.split("=", 1)[1] for ln in text.splitlines() if "=" in ln and not ln.startswith("#")}
    tfs = lines["TemporaryFileSystem"].split()
    assert "/etc/awg-bot:ro" in tfs and "/var/lib/awg-bot:ro" in tfs, f"каталоги резерва не закрыты: {tfs}"
    assert lines["LogNamespace"] == "awg-guest"
    assert lines["MemorySwapMax"] == "0"
    assert lines["LimitCORE"] == "0"
    assert lines["NoNewPrivileges"] == "yes" and lines["ProtectSystem"] == "strict"
    assert lines["Environment"] and "AWG_BOT_GUEST=1" in text, "без AWG_BOT_GUEST бот поднялся бы как основной"
    assert "AWG_BOT_CONF_DIR=/run/awg-bot-guest/conf" in text and "AWG_BOT_DATA_DIR=/run/awg-bot-guest/data" in text, \
        "конфиг и данные гостя — в хранилище в памяти"
    assert lines["ReadWritePaths"].split() == ["/run/awg-bot-guest", "/var/lib/awg-bot-guest"], \
        "писать гость может только в хранилище и в каталог rep.bin"


@pytest.mark.parametrize("args", [(), ("--nope",), ("plan",)])
def test_an_unknown_mode_is_a_usage_error(box, args):
    r = box.run(*args)
    assert r.returncode == 2 and "--plan" in r.stdout
