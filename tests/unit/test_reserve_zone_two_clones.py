"""Два клона в гостевой зоне (install/reserve-zone-setup.sh): клиентский
интерфейс основы и интерфейс окна переезда профилей.

`--take` поднимает первый клон из stdin и следом — клоны GUEST_EXTRA («имя=cidr
…») из конфигов, которые процесс открытия уже положил в хранилище; конфига нет
или клон не встал — строка ВНИМАНИЕ, переезд идёт без него. В состоянии —
`iface`/`port` первого и `ifaces`/`ports` всех; таблица зоны рисует правила
набором интерфейсов; `--restart-clone`, `--relay`, `--teardown` идут по всем
клонам (`state_ifaces`, старое состояние знало только `iface`).

Режимы гоняются bash'ем по функциям самого скрипта (всё до разбора режимов),
с подставными `ip`, `awg`, `nft`, `sysctl`, `systemctl` в PATH; монтирование
хранилища, юниты и настройки журнала (пишут в /etc) заменены пустыми
функциями. Хост не трогается.

Цена ошибки: второй клон не поднят — устройства, уже переведённые на новый
интерфейс, без VPN всё время переезда; не снят при возврате — интерфейс с
ключом основы живёт в зоне резерва; правила зоны только для первого клона —
DNS и выход наружу у второго закрыты; отказ `--take` из-за второго
интерфейса — переезд не состоялся вовсе, хотя клиентский интерфейс поднять
было можно.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import fake_cmd

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "install" / "reserve-zone-setup.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")
LIB = TEXT[:TEXT.rindex('\ncase "${1:-}" in')]          # функции и переменные — без разбора режимов


def _conf(port: int, n: int) -> str:
    return (f"[Interface]\nAddress = 10.8.{n}.1/24\nListenPort = {port}\nPrivateKey = DUMMY-priv-{n}\nMTU = 1376\n"
            f"Jc = 4\nH1 = 100-200\n\n[Peer]\nPublicKey = DUMMY-pub-{n}\nAllowedIPs = 10.8.{n}.2/32\n")


class _Zone:
    """Хост моделью: журнал вызовов, таблица зоны (последний `nft -f -`),
    копии конфигов, отданных `awg setconf`; AWG_FAIL — подстрока пути конфига,
    на котором setconf отказывает."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.log = tmp / "calls.log"
        self.log.write_text("", encoding="utf-8")
        self.nft = tmp / "nft.txt"
        self.run_dir = tmp / "run"
        self.state_dir = tmp / "state"
        self.ns = tmp / "ns"
        self.ns.write_text("")                              # оболочка зоны есть
        self.setconfs = tmp / "setconfs"
        self.setconfs.mkdir()
        fake_cmd(self.bin / "ip",
                 f'echo "ip $*" >> {self.log}\n'
                 'if [ "$1 $2" = "netns exec" ]; then shift 3; exec "$@"; fi\n'
                 'if [ "$1 $2" = "link show" ]; then exit 1; fi\n'
                 'if [ "$1 $2" = "rule del" ]; then exit 1; fi\n'
                 'exit 0\n')
        fake_cmd(self.bin / "awg",
                 f'echo "awg $*" >> {self.log}\n'
                 'if [ "$1" = setconf ]; then\n'
                 '  case "$3" in *"${AWG_FAIL:-@@}"*) exit 1 ;; esac\n'
                 f'  cp "$3" {self.setconfs}/$(basename "$3")\n'
                 'fi\nexit 0\n')
        fake_cmd(self.bin / "nft", f'if [ "$1 $2" = "-f -" ]; then cat > {self.nft}; else echo "nft $*" >> {self.log}; fi\n'
                                   'exit 0\n')
        for name in ("sysctl", "systemctl"):
            fake_cmd(self.bin / name, f'echo "{name} $*" >> {self.log}\nexit 0\n')

    def run(self, func: str, *, stdin: str = "", **env: str) -> subprocess.CompletedProcess:
        driver = (LIB + "\n"
                  f'NS="{self.ns}"\n'
                  'need_root() { :; }\n'
                  'store_mount() { mkdir -p "$RUN/data" "$RUN/conf" "$RUN/awg"; }\n'
                  'write_dnsmasq() { :; }; write_journal_conf() { :; }; write_units() { :; }\n'
                  f'{func}\n')
        pydir = os.path.dirname(sys.executable)           # state_write зовёт python3
        full = {"PATH": f"{self.bin}:{pydir}:/usr/bin:/bin", "RSV_ZONE": "awgtst-нет-такой",
                "RSV_RUN": str(self.run_dir), "RSV_VAR": str(self.tmp / "var"), "RSV_STATE_DIR": str(self.state_dir)}
        full.update(env)
        return subprocess.run(["bash", "-c", driver], input=stdin, capture_output=True, text=True, timeout=30, env=full)

    def take(self, *, extra: str = "", extra_confs: dict | None = None, **env) -> subprocess.CompletedProcess:
        (self.run_dir / "awg").mkdir(parents=True, exist_ok=True)
        for name, text in (extra_confs or {}).items():     # конфиги вторых интерфейсов кладёт процесс открытия
            (self.run_dir / "awg" / f"{name}.conf").write_text(text, encoding="utf-8")
        return self.run("cmd_take", stdin=_conf(45877, 1), GUEST_IF="awg0", GUEST_ADDR="10.8.1.1/24",
                        GUEST_EXTRA=extra, **env)

    def state(self) -> dict:
        try:
            return json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))
        except OSError:
            return {}

    def put_state(self, **kv) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "state.json").write_text(json.dumps(kv), encoding="utf-8")

    def calls(self, prefix: str) -> list[str]:
        return [ln for ln in self.log.read_text(encoding="utf-8").splitlines() if ln.startswith(prefix)]

    def table(self) -> list[str]:
        return [ln.strip() for ln in self.nft.read_text(encoding="utf-8").splitlines()] if self.nft.exists() else []


@pytest.fixture()
def zone(tmp_path):
    return _Zone(tmp_path)


def test_the_script_parses():
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


# ── --take ──────────────────────────────────────────────────────────────────

def test_take_raises_both_clones_and_writes_both_into_the_state(zone):
    r = zone.take(extra="awg1=10.8.2.1/24", extra_confs={"awg1": _conf(45878, 2)})
    assert r.returncode == 0 and "take: ok" in r.stdout, r.stdout + r.stderr
    st = zone.state()
    assert (st.get("iface"), st.get("port"), st.get("ifaces"), st.get("ports")) == ("awg0", "45877", "awg0 awg1", "45877 45878"), st
    renames = [c for c in zone.calls("ip link set awgc") if " name " in c]
    assert [c.split()[-1] for c in renames] == ["awg0", "awg1"], f"клоны в зоне не под именами основы: {renames}"
    assert "ip addr replace 10.8.2.1/24 dev awg1" in zone.calls("ip addr"), "второму клону не дан его адрес"


def test_the_configs_given_to_setconf_have_no_awg_quick_keys_for_both_clones(zone):
    """`awg setconf` отвечает «Line unrecognized» на Address/MTU/DNS/Table —
    клон с таким конфигом не поднимается."""
    zone.take(extra="awg1=10.8.2.1/24", extra_confs={"awg1": _conf(45878, 2) + "DNS = 10.8.2.1\nTable = off\n"})
    for name in ("awg0", "awg1"):
        text = (zone.setconfs / f"{name}.setconf").read_text(encoding="utf-8")
        keys = {ln.split("=")[0].strip() for ln in text.splitlines() if "=" in ln}
        assert not keys & {"Address", "MTU", "DNS", "Table"}, f"{name}: в конфиге setconf {sorted(keys)}"
        assert {"PrivateKey", "ListenPort", "Jc", "H1", "PublicKey", "AllowedIPs"} <= keys, f"{name}: снято лишнее: {keys}"


def test_the_zone_table_has_the_rules_for_both_clones_as_one_set(zone):
    zone.take(extra="awg1=10.8.2.1/24", extra_confs={"awg1": _conf(45878, 2)})
    t = zone.table()
    for rule in ('iifname { "awg0", "awg1" } udp dport 53 accept', 'iifname { "awg0", "awg1" } tcp dport 53 accept',
                 'iifname { "awg0", "awg1" } oifname { "awg0", "awg1" } drop',
                 'iifname { "awg0", "awg1" } oifname { "awggv-n", "awggr-n", "awglink*" } accept',
                 'iifname "awglink*" oifname { "awg0", "awg1" } ct state established,related accept'):
        assert rule in t, f"нет правила «{rule}»:\n" + "\n".join(t)


def test_without_extra_interfaces_there_is_one_clone_and_a_one_element_set(zone):
    r = zone.take()
    assert r.returncode == 0, r.stdout + r.stderr
    st = zone.state()
    assert (st.get("ifaces"), st.get("ports")) == ("awg0", "45877"), st
    assert 'iifname { "awg0" } udp dport 53 accept' in zone.table(), "\n".join(zone.table())
    assert len(zone.calls("awg setconf")) == 1


def test_an_extra_interface_without_its_config_is_a_warning_not_a_refusal(zone):
    r = zone.take(extra="awg1=10.8.2.1/24")
    assert r.returncode == 0 and "take: ok" in r.stdout, r.stdout + r.stderr
    assert "ВНИМАНИЕ: конфига awg1 нет" in r.stdout, r.stdout
    st = zone.state()
    assert (st.get("ifaces"), st.get("ports")) == ("awg0", "45877"), f"несуществующий клон в состоянии: {st}"
    assert 'iifname { "awg0" } udp dport 53 accept' in zone.table()


def test_an_extra_clone_that_does_not_come_up_is_a_warning_and_is_left_out(zone):
    r = zone.take(extra="awg1=10.8.2.1/24", extra_confs={"awg1": _conf(45878, 2)}, AWG_FAIL="awg1")
    assert r.returncode == 0 and "ВНИМАНИЕ: клон awg1 не поднялся" in r.stdout, r.stdout + r.stderr
    assert zone.state().get("ifaces") == "awg0", zone.state()


def test_the_first_clone_failing_refuses_the_take(zone):
    r = zone.take(extra="awg1=10.8.2.1/24", extra_confs={"awg1": _conf(45878, 2)}, AWG_FAIL="awg0")
    assert r.returncode != 0 and "клон awg0 не поднялся" in r.stderr, r.stdout + r.stderr
    assert zone.state().get("phase") != "taking", "клиентский клон не встал, а зона «принимает»"


@pytest.mark.parametrize("extra", ["awg1", "=10.8.2.1/24", "awg1="])
def test_a_malformed_extra_pair_is_skipped(zone, extra):
    r = zone.take(extra=extra, extra_confs={"awg1": _conf(45878, 2)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert zone.state().get("ifaces") == "awg0", zone.state()


# ── по всем клонам ──────────────────────────────────────────────────────────

def _confs(zone):
    (zone.run_dir / "awg").mkdir(parents=True, exist_ok=True)
    for name, port, n in (("awg0", 45877, 1), ("awg1", 45878, 2)):
        (zone.run_dir / "awg" / f"{name}.conf").write_text(_conf(port, n), encoding="utf-8")


def test_restart_clone_goes_through_every_clone(zone):
    _confs(zone)
    zone.put_state(phase="serving", iface="awg0", ifaces="awg0 awg1", ports="45877 45878")
    r = zone.run("cmd_restart_clone")
    assert r.returncode == 0 and "clone: restarted" in r.stdout, r.stdout + r.stderr
    assert [c.split()[2] for c in zone.calls("awg setconf")] == ["awg0", "awg1"], zone.calls("awg")
    assert "ip link set awg1 up" in zone.calls("ip link set"), "второй клон оставлен опущенным"


def test_restart_clone_with_an_old_state_restarts_the_single_clone(zone):
    _confs(zone)
    zone.put_state(phase="serving", iface="awg0")
    assert zone.run("cmd_restart_clone").returncode == 0
    assert [c.split()[2] for c in zone.calls("awg setconf")] == ["awg0"]


def test_restart_clone_without_clones_refuses(zone):
    zone.put_state(phase="idle")
    r = zone.run("cmd_restart_clone")
    assert r.returncode != 0 and "клона нет" in r.stderr


@pytest.mark.parametrize("func", ["cmd_relay", "cmd_teardown"])
def test_relay_and_teardown_remove_every_clone(zone, func):
    zone.put_state(phase="serving", iface="awg0", ifaces="awg0 awg1", ports="45877 45878", port="45877")
    r = zone.run(func, RELAY_TO="203.0.113.5:45877")
    assert r.returncode == 0, r.stdout + r.stderr
    dels = [c.split()[-1] for c in zone.calls("ip link del") if c.split()[-1] in ("awg0", "awg1")]
    assert dels == ["awg0", "awg1"], f"клоны не сняты: {zone.calls('ip link del')}"
    st = zone.state()
    assert (st.get("iface"), st.get("ifaces")) == ("", ""), st
    if func == "cmd_relay":
        assert st.get("ports") == "45877 45878", "в relay порты нужны пересылке хвоста — а они стёрты"
    else:
        assert (st.get("ports"), st.get("port")) == ("", ""), st


def test_the_clones_list_falls_back_to_iface_for_an_old_state(zone):
    zone.put_state(iface="awg0")
    assert zone.run('printf "[%s]" "$(state_ifaces)"').stdout == "[awg0]"
    zone.put_state(iface="awg0", ifaces="awg0 awg1")
    assert zone.run('printf "[%s]" "$(state_ifaces)"').stdout == "[awg0 awg1]"


def test_a_wipe_outside_relay_leaves_no_clones_in_the_state(zone):
    """`--wipe` вне relay сбрасывает зону в idle (iface пуст) — список клонов
    тоже обязан опустеть: иначе следующий `--shell` рисует правила для клонов,
    которых нет, а `--restart-clone` дёргает несуществующие интерфейсы."""
    zone.put_state(phase="serving", iface="awg0", ifaces="awg0 awg1", ports="45877 45878")
    r = zone.run("cmd_wipe")
    assert r.returncode == 0, r.stdout + r.stderr
    st = zone.state()
    assert st.get("phase") == "idle" and st.get("iface") == "", st
    assert zone.run('printf "[%s]" "$(state_ifaces)"').stdout == "[]", f"после wipe в состоянии клоны: {st}"
    assert (st.get("ports"), st.get("port")) == ("", ""), f"после wipe порты клонов остались: {st}"


def test_a_wipe_in_relay_keeps_the_ports_for_the_tail_forwarding(zone):
    zone.put_state(phase="relay", relay_to="203.0.113.5:45877", ports="45877 45878", port="45877")
    assert zone.run("cmd_wipe").returncode == 0
    assert zone.state().get("ports") == "45877 45878", "wipe в relay снял порты — хвост второго интерфейса не пересылается"


def test_status_names_every_clone(zone):
    zone.put_state(phase="serving", iface="awg0", ifaces="awg0 awg1")
    r = zone.run("cmd_status")
    assert "iface=awg0" in r.stdout.splitlines() and "ifaces=awg0 awg1" in r.stdout.splitlines(), r.stdout
