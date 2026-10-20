"""CAKE на внешнем интерфейсе резерва в переезде (install/reserve-zone-setup.sh:
cake_on при `--serve`, cake_off при `--wipe`): `besteffort dual-dsthost nat`
без цифр скорости, если модуль `sch_cake` есть; прежняя дисциплина
записана в состояние зоны и возвращается при стирании; без модуля —
дисциплина не тронута. И снятие гостя (`--teardown`, `--wipe`): правило
`ip rule iif awggr-h` РФ-трафика зоны снимается всё, сколько бы его ни
поставили.

Скрипт гоняется целиком bash'ем; `id` (root), `modinfo`, `ip` (маршрут по
умолчанию JSON-ом, остальное — журнал), `tc` (дисциплина интерфейса —
файл-модель: `show` печатает, `replace`/`del` меняют), `systemctl`,
`mountpoint`, `umount` подменены в PATH; состояние зоны — во временном
каталоге.

Цена ошибки: CAKE, оставшийся после возврата, — дисциплина хоста резерва
сменена навсегда без ведома его админа; прежняя дисциплина, потерянная на
повторном `--serve`, — после стирания у интерфейса не та очередь, что была;
CAKE без модуля — `tc` отказывает, а состояние говорит «включено»;
правило iif awggr-h после снятия гостя — таблица РФ-доступа резерва ловит
трафик несуществующего veth при следующем переезде дважды.
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
ORIG = "qdisc fq_codel 0: root refcnt 2 limit 10240p flows 1024 quantum 1514 target 5ms interval 100ms"


class _Host:
    def __init__(self, tmp: Path, *, cake: bool = True, wan: str = "eth0"):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.qdisc = tmp / "qdisc"                    # строка `tc qdisc show dev <wan>` (первая)
        self.qdisc.write_text(ORIG + "\n", encoding="utf-8")
        self.log = tmp / "calls.log"
        self.log.write_text("", encoding="utf-8")
        self.state_dir = tmp / "state"
        fake_cmd(self.bin / "id", 'case "$1" in -u) echo 0 ;; *) echo "uid=0" ;; esac\n')
        fake_cmd(self.bin / "modinfo", ('[ "$1" = sch_cake ] && exit 0\n' if cake else "") + "exit 1\n")
        route = json.dumps([{"dst": "default", "gateway": "203.0.113.1", "dev": wan}] if wan else [])
        fake_cmd(self.bin / "ip",
                 f'echo "ip $*" >> {self.log}\n'
                 f"if [ \"$1 $2 $3 $4\" = \"-j route show default\" ]; then echo '{route}'; exit 0; fi\n"
                 f'if [ "$1 $2" = "rule del" ]; then n=$(cat {tmp}/rules 2>/dev/null || echo 0); '
                 f'[ "$n" -gt 0 ] || exit 1; echo $((n - 1)) > {tmp}/rules; exit 0; fi\n'
                 'exit 0\n')
        # tc: show — модель; replace dev X root KIND ARGS — новая строка; del — дисциплина ядра по умолчанию
        fake_cmd(self.bin / "tc",
                 f'echo "tc $*" >> {self.log}\n'
                 f'if [ "$1 $2" = "qdisc show" ]; then cat {self.qdisc}; exit 0; fi\n'
                 f'if [ "$1 $2" = "qdisc replace" ]; then shift 5; k="$1"; shift; '
                 f'echo "qdisc $k 8001: root refcnt 2 $*" > {self.qdisc}; exit 0; fi\n'
                 f'if [ "$1 $2" = "qdisc del" ]; then echo "qdisc pfifo_fast 0: root refcnt 2 bands 3" > {self.qdisc}; exit 0; fi\n'
                 'exit 0\n')
        for name in ("systemctl", "umount"):
            fake_cmd(self.bin / name, f'echo "{name} $*" >> {self.log}\nexit 0\n')
        fake_cmd(self.bin / "mountpoint", "exit 1\n")

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        pydir = os.path.dirname(sys.executable)          # state_write зовёт python3
        full = {"PATH": f"{self.bin}:{pydir}:/usr/bin:/bin", "RSV_ZONE": "awgtst-нет-такой",
                "RSV_RUN": str(self.tmp / "run"), "RSV_VAR": str(self.tmp / "var"),
                "RSV_STATE_DIR": str(self.state_dir)}
        full.update(env)
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=30, env=full)

    def state(self) -> dict:
        try:
            return json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))
        except OSError:
            return {}

    def put_state(self, **kv) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "state.json").write_text(json.dumps(kv), encoding="utf-8")

    def show(self) -> str:
        return self.qdisc.read_text(encoding="utf-8").strip()

    def tc_calls(self) -> list[str]:
        return [ln for ln in self.log.read_text(encoding="utf-8").splitlines() if ln.startswith("tc ")]


@pytest.fixture()
def host(tmp_path):
    h = _Host(tmp_path)
    h.put_state(phase="taking", iface="awg0")
    return h


def test_serve_puts_cake_without_bandwidth_on_the_wan_and_remembers_the_old_discipline(host):
    r = host.run("--serve", WEBHOOK_PORT="443")
    assert r.returncode == 0 and "serve: ok" in r.stdout, r.stdout + r.stderr
    assert host.show().startswith("qdisc cake ") and "besteffort dual-dsthost nat" in host.show(), host.show()
    assert "bandwidth" not in host.show(), "CAKE с цифрой скорости — решение было «без bandwidth»"
    st = host.state()
    assert (st.get("cake"), st.get("cake_dev"), st.get("cake_prev")) == ("on", "eth0", "fq_codel"), st
    assert st.get("phase") == "serving"


def test_wipe_brings_the_old_discipline_back(host):
    host.run("--serve", WEBHOOK_PORT="443")
    r = host.run("--wipe")
    assert r.returncode == 0 and "wipe: ok" in r.stdout, r.stdout + r.stderr
    assert host.show().split()[1] == "fq_codel", f"после стирания на интерфейсе не прежняя дисциплина: {host.show()}"
    st = host.state()
    assert (st.get("cake"), st.get("cake_dev"), st.get("cake_prev")) == ("off", "", "")


def test_a_repeated_serve_keeps_the_original_discipline_for_the_wipe(host):
    """Второй `--serve` в том же переезде (повтор процесса, перезапуск бота)
    видит на интерфейсе уже CAKE: прежней дисциплиной должна остаться та,
    что была до переезда, а не сам CAKE."""
    host.run("--serve", WEBHOOK_PORT="443")
    host.run("--serve", WEBHOOK_PORT="443")
    assert host.state().get("cake_prev") == "fq_codel", f"прежняя дисциплина затёрта: {host.state()}"
    host.run("--wipe")
    assert host.show().split()[1] == "fq_codel", f"после стирания — не та очередь, что была: {host.show()}"


def test_without_the_module_the_discipline_is_not_touched(tmp_path):
    h = _Host(tmp_path, cake=False)
    h.put_state(phase="taking", iface="awg0")
    r = h.run("--serve", WEBHOOK_PORT="443")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not [c for c in h.tc_calls() if "replace" in c or "del" in c], h.tc_calls()
    assert h.show() == ORIG and h.state().get("cake") == "absent"
    h.run("--wipe")
    assert h.show() == ORIG, "стирание тронуло дисциплину, которую CAKE не менял"


def test_without_a_default_route_cake_is_not_put(tmp_path):
    h = _Host(tmp_path, wan="")
    h.put_state(phase="taking", iface="awg0")
    assert h.run("--serve", WEBHOOK_PORT="443").returncode == 0
    assert h.show() == ORIG and not h.state().get("cake_dev")


@pytest.mark.parametrize("cake, want", [(True, "cake=yes"), (False, "cake=no")])
def test_the_plan_says_whether_cake_is_there_and_touches_nothing(tmp_path, cake, want):
    h = _Host(tmp_path, cake=cake)
    out = h.run("--plan").stdout.splitlines()
    assert want in out, out
    assert not h.tc_calls() and h.show() == ORIG, "план тронул дисциплину"


@pytest.mark.parametrize("mode", ["--teardown", "--wipe"])
def test_stopping_the_guest_removes_every_rf_rule_of_the_zone_veth(tmp_path, mode):
    h = _Host(tmp_path)
    h.put_state(phase="serving", iface="awg0")
    (tmp_path / "rules").write_text("2\n", encoding="utf-8")          # правило стояло дважды (повтор --take)
    r = h.run(mode)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "rules").read_text(encoding="utf-8").strip() == "0", "правило iif awggr-h осталось"
    dels = [ln for ln in h.log.read_text(encoding="utf-8").splitlines() if ln.startswith("ip rule del")]
    assert dels and all("iif awggr-h lookup 100" in ln for ln in dels), dels
    assert "ip link del awggr-h" in h.log.read_text(encoding="utf-8"), "veth РФ-трафика зоны не снят"
