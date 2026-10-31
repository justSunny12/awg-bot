"""`awg-bot uninstall`: хвост (`__post_uninstall`) идёт копией скрипта из
временного каталога, когда каталога установки уже нет. awg-bot.sh читает свои
части при разборе (`. "$SELF_DIR/install/lib/update.sh"`, `restore.sh`), и
копия обязана уехать вместе с ними: одинокая копия в /tmp падала на первой же
строке чтения — хвост не выполнялся вовсе.

Здесь копию делает настоящий `cmd_uninstall` из настоящей поставки, разложенной
во временный «каталог установки», а потом копия запускается сама по себе, из
другого каталога (`help` — без действий на машине прогона). Плюс: снятие без
app.yaml не падает молча под pipefail (роль — пусто, строка об этом), хвост
под `set -e` доходит до конца, даже когда интерфейса зонда уже нет, и в конце
убирает временный каталог целиком.

Цена ошибки: `awg-bot uninstall` снимает сервис и код, а данные и секреты не
спрошены, линк шлюза и его юнит, обвязка РФ-доступа, линк пары, гостевая зона
с расшифрованными данными основы в памяти, пространство зондов кворума и
таблицы nft остаются на хосте; переустановка встаёт поверх мусора.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_reserve_uninstall import CLI, _Host, _func, _rooted

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _stage(tmp: Path, *, role: str | None) -> Path:
    """Каталог установки из настоящей поставки: awg-bot.sh, install/lib, скрипт зоны."""
    inst = tmp / "opt" / "awg-bot"
    (inst / "install" / "lib").mkdir(parents=True)
    shutil.copy(ROOT / "awg-bot.sh", inst / "awg-bot.sh")
    for lib in (ROOT / "install" / "lib").glob("*.sh"):
        shutil.copy(lib, inst / "install" / "lib" / lib.name)
    (inst / "install" / "reserve-zone-setup.sh").write_text("# DUMMY zone script\n", encoding="utf-8")
    (tmp / "etc" / "conf").mkdir(parents=True)
    if role is not None:
        (tmp / "etc" / "conf" / "app.yaml").write_text(f'role: "{role}"\n', encoding="utf-8")
    return inst


def _uninstall(tmp: Path, *, role: str | None) -> tuple[subprocess.CompletedProcess, dict]:
    inst = _stage(tmp, role=role)
    seen = tmp / "exec.txt"
    (tmp / "t").mkdir()
    prog = "\n".join([
        "set -euo pipefail",
        f'INSTALL_DIR="{inst}"; ETC_DIR="{tmp / "etc"}"; SERVICE=awg-bot',
        f'SELF_PATH="{inst}/awg-bot.sh"; SELF_DIR="{inst}"',
        f'UNIT_PATH="{tmp / "unit"}"; SELF_LINK="{tmp / "link"}"',
        "require_root() { :; }; confirm() { return 0; }; systemctl() { :; }; update_guard_disarm() { :; }",
        'warn() { echo "WARN $*" >&2; }; ok() { :; }; log() { :; }',
        # exec — журнал: настоящий хвост на машине прогона не запускается
        f'exec() {{ {{ echo "role=$AWG_ROLE"; echo "zone=$AWG_ZONE_SCRIPT"; echo "dir=$AWG_UNINSTALL_DIR"; '
        f'echo "argv=$*"; }} > "{seen}"; }}',
        _func(CLI, "cmd_uninstall"), "cmd_uninstall"])
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", "TMPDIR": str(tmp / "t")})
    got = {}
    if seen.exists():
        for line in seen.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            got[k] = v
    return r, got


def test_the_tail_copy_runs_on_its_own_after_the_install_dir_is_gone(tmp_path):
    r, got = _uninstall(tmp_path, role="client")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (tmp_path / "opt" / "awg-bot").exists(), "предусловие: код снят до хвоста"
    argv = got["argv"].split()
    assert argv[0] == "bash" and argv[-1] == "__post_uninstall", got
    copy = Path(argv[1])
    assert copy.is_file() and copy.parent == Path(got["dir"]), got
    assert (copy.parent / "install" / "lib" / "update.sh").is_file()
    assert (copy.parent / "install" / "lib" / "restore.sh").is_file()
    assert got["zone"] and Path(got["zone"]).read_text(encoding="utf-8") == "# DUMMY zone script\n"
    assert Path(got["zone"]).parent == copy.parent, "копия зоны — в том же каталоге, уходит вместе с ним"
    # копия читает свои части при разборе: запуск из постороннего каталога
    # должен дойти до разбора глагола, а не упасть на `. …/install/lib/…`
    run = subprocess.run(["bash", str(copy), "help"], capture_output=True, text=True, timeout=30,
                         cwd="/", env={"PATH": "/usr/bin:/bin"})
    assert run.returncode == 0, run.stdout + run.stderr
    assert "awg-bot uninstall" in run.stdout


def test_the_tail_copy_dir_is_private(tmp_path):
    _r, got = _uninstall(tmp_path, role="client")
    assert (Path(got["dir"]).stat().st_mode & 0o077) == 0, "каталог копии читают посторонние"


@pytest.mark.parametrize("role,want", [("gateway", "gateway"), (None, "client")], ids=["gateway", "no-app-yaml"])
def test_the_role_reaches_the_tail_and_a_missing_app_yaml_is_not_a_silent_exit(tmp_path, role, want):
    r, got = _uninstall(tmp_path, role=role)
    assert r.returncode == 0, f"снятие оборвалось кодом {r.returncode}: {r.stderr}"
    assert got.get("role") == want, got
    if role is None:
        assert "роль установки не прочитана" in r.stderr, r.stderr


# ── хвост: под set -e до конца и уборка временного каталога ─────────────────

def _tail(h: _Host, uninstall_dir: Path, *, ip_addr_fails: bool) -> subprocess.CompletedProcess:
    fail = "return 1" if ip_addr_fails else "return 0"
    prog = "\n".join([
        "set -euo pipefail", h.stubs(),
        f'ip() {{ _j "ip $*"; [ "$1 $2" = "-4 addr" ] && {fail}; return 1; }}',
        'awg() { return 0; }',
        'setsid() { _j "setsid $*"; }',
        f'DATA_DIR="{h.tmp}/data"; ETC_DIR="{h.tmp}/etc-awg-bot"',
        f'SELF_DIR="{uninstall_dir}"; SELF_PATH="{uninstall_dir}/awg-bot.sh"',
        f'AWG_UNINSTALL_DIR="{uninstall_dir}"', "AWG_ROLE=client", "unset AWG_ZONE_SCRIPT",
        _rooted(_func(CLI, "cmd_post_uninstall")), "cmd_post_uninstall", "wait"])
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin"})


@pytest.fixture()
def probe_host(tmp_path):
    h = _Host(tmp_path)
    h.put("/etc/amnezia/amneziawg/awg0.conf",
          "[Interface]\nAddress = 10.8.1.1/24\nAddress = 169.254.88.1/24\n\n"
          "[Peer]\nPublicKey = DUMMY\nAllowedIPs = 169.254.88.2/32\n").chmod(0o600)
    d = tmp_path / "awg-bot-uninstall.X"
    d.mkdir()
    return h, Path(str(d.resolve()))


def test_the_tail_survives_a_vanished_probe_interface_under_errexit(probe_host):
    """`ip -4 addr show dev …` упал (интерфейса уже нет) — под pipefail и set -e
    конвейер обрывал хвост посреди зачистки."""
    h, d = probe_host
    r = _tail(h, d, ip_addr_fails=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK Готово." in r.stdout, r.stdout
    path = h.root / "etc/amnezia/amneziawg/awg0.conf"
    assert "169.254.88." not in path.read_text(encoding="utf-8"), "пиры зондов и адрес ответчика остались в конфиге"
    assert (path.stat().st_mode & 0o077) == 0, f"конфиг с приватным ключом стал {oct(path.stat().st_mode)}"


def test_the_tail_removes_its_whole_temp_dir_last(probe_host):
    h, d = probe_host
    r = _tail(h, d, ip_addr_fails=False)
    assert r.returncode == 0, r.stdout + r.stderr
    last = [c for c in h.calls() if c.startswith("setsid ")]
    assert last == [f"setsid sh -c sleep 1; rm -rf '{d}'"], last


def test_a_tail_not_run_from_its_temp_dir_removes_only_itself(probe_host, tmp_path):
    """Каталог в AWG_UNINSTALL_DIR — не каталог запущенной копии: целиком не сносим."""
    h, _d = probe_host
    other = tmp_path / "other"
    other.mkdir()
    prog_dir = Path(str((tmp_path / "awg-bot-uninstall.X").resolve()))
    r = subprocess.run(["bash", "-c", "\n".join([
        "set -euo pipefail", h.stubs(), 'ip() { return 1; }', 'awg() { return 0; }',
        'setsid() { _j "setsid $*"; }',
        f'DATA_DIR="{h.tmp}/data"; ETC_DIR="{h.tmp}/etc-awg-bot"',
        f'SELF_DIR="{prog_dir}"; SELF_PATH="{prog_dir}/awg-bot.sh"', f'AWG_UNINSTALL_DIR="{other}"',
        "AWG_ROLE=client", "unset AWG_ZONE_SCRIPT",
        _rooted(_func(CLI, "cmd_post_uninstall")), "cmd_post_uninstall", "wait"])],
        capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stdout + r.stderr
    last = [c for c in h.calls() if c.startswith("setsid ")]
    assert last == [f"setsid sh -c sleep 1; rm -f '{prog_dir}/awg-bot.sh'"], last


# ── снос установщиком — тот же список, что хвост uninstall ──────────────────

_TEARDOWN = (r"ip netns del [\w-]+", r"ip rule del [\w -]+?(?= 2>)", r"[\w.-]+\.sh\"? --[\w-]+",
             r"nft delete table inet \w+", r"ip link del \w+", r"systemctl (?:disable --now|stop) [\w@ .-]+?(?= 2>)")


def _teardown_steps(body: str) -> set[str]:
    out: set[str] = set()
    for pat in _TEARDOWN:
        out |= {m.replace('"', "").strip() for m in re.findall(pat, body)}
    return out


def test_the_wiping_installer_takes_down_everything_the_uninstall_tail_does():
    """`wipe_previous` обещает «то же, что awg-bot uninstall с ответами «да»»:
    каждый шаг снятия хвоста (пространства, ip rule, скрипты обвязки с режимом
    снятия, таблицы nft, юниты) есть и у сноса. Расхождение — линк к резерву
    шлюза, пространство зондов кворума или правило транзита РФ второй стороны
    переживали «удалить вместе с данными» и мешали чистой установке."""
    from tests.unit.test_reserve_uninstall import INSTALLER
    tail = _teardown_steps(_func(CLI, "cmd_post_uninstall"))
    wipe = _teardown_steps(_func(INSTALLER, "wipe_previous"))
    assert {"ip netns del awg-quo", "ip rule del pref 151", "reserve-gw-setup.sh --remove"} <= tail, tail
    assert not tail - wipe, f"снос установщиком не делает: {sorted(tail - wipe)}"


def test_the_installer_reads_a_missing_app_yaml_as_an_empty_role(tmp_path):
    from tests.unit.test_reserve_uninstall import INSTALLER
    prog = "\n".join(["set -euo pipefail", f'ETC_DIR="{tmp_path}/etc"', 'log() { echo "LOG $*"; }',
                      _func(INSTALLER, "installed_role"), 'r="$(installed_role)"', 'echo "role=[$r]"'])
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, f"установщик оборвался молча кодом {r.returncode}"
    assert "role=[]" in r.stdout and "не прочитана" in r.stderr, r.stdout + r.stderr
