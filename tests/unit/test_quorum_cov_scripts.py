"""Скрипты обхода Telegram агентов шлюзов: `reserve-gw-setup.sh --tg on|off`
(набор tg_nets4, метка 0x53 сокетам процесса агента по cgroup юнита, маскарад и
кламп MSS в awgrsv, `ip rule fwmark 0x53 lookup 152`; откат блока, если nft его не
принял) и блок tg зоны в `reserve-zone-setup.sh` (veth awggx, маршруты к набору
Telegram в таблице 153 по источнику — шлюзовым сторонам линков /32, пересылка
только tcp 443 с пределом соединений, кламп MSS).

Скрипты гоняются под sh с подставными `nft`, `ip`, `awg-quick` и прочими (хосты
tests/unit/test_reserve_gw_script.py и tests/unit/test_reserve_zone_netmap.py);
каталог cgroup юнита — копия скрипта с путём `/sys/fs/cgroup/` во временный
каталог (на машине прогона его нет); правила `ip rule` — файл.

Цена ошибки: метка на всех сокетах хоста, а не только процесса агента, —
весь трафик шлюза уходит во второй сервер; блок без cgroup — nft отвергает
таблицу целиком, и обвязка линка к резерву не встаёт вовсе; таблица, отвергнутая
с блоком, без отката — то же; правило 0x53, оставшееся после `--tg off`, — метки
уже нет, но следующая сборка с блоком без `--tg on` уводит Bot API в мёртвый линк;
пересылка зоны с линков не только на 443 набора Telegram — второй сервер
становится открытым выходом в интернет для любого шлюза основы.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import SH, fake_cmd
from tests.unit.test_reserve_gw_script import SCRIPT, _Gw, _chain, _set
from tests.unit.test_reserve_zone_netmap import _Zone

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CGROUP = "system.slice/awg-bot.service"


def _nets(path: Path) -> list[str]:
    m = re.search(r'^TG_NETS="([^"]+)"', path.read_text(encoding="utf-8"), re.M)
    assert m, f"нет TG_NETS в {path.name}"
    return m.group(1).split()


TG_NETS = _nets(ROOT / "install" / "reserve-gw-setup.sh")


class _TgGw(_Gw):
    """Хост линка к резерву с каталогом cgroup во временном каталоге и точным
    `ip rule del` (по метке, а не всё сразу)."""

    def __init__(self, tmp: Path):
        super().__init__(tmp)
        self.cg = tmp / "cgroup"
        self.script = tmp / "reserve-gw-setup.sh"
        self.script.write_text(SCRIPT.read_text(encoding="utf-8").replace("/sys/fs/cgroup/", f"{self.cg}/"),
                               encoding="utf-8")
        self.reject = tmp / "nft.reject"
        fake_cmd(self.bin / "ip",
                 f'echo "ip $*" >> {self.log}\n'
                 f'if [ "$1 $2" = "link show" ]; then [ -f {self.up} ]; exit $?; fi\n'
                 f'if [ "$1 $2" = "rule list" ]; then cat {self.rules}; exit 0; fi\n'
                 f'if [ "$1 $2" = "rule add" ]; then echo "100: from all fwmark $4 lookup $6" >> {self.rules}; exit 0; fi\n'
                 f'if [ "$1 $2" = "rule del" ]; then grep -v "fwmark $4 lookup" {self.rules} > {self.rules}.t; '
                 f'mv {self.rules}.t {self.rules}; exit 0; fi\n'
                 'exit 0\n')
        fake_cmd(self.bin / "nft",
                 f'echo "nft $*" >> {self.log}\n'
                 f'if [ "$1" = "-f" ] && [ "$2" = "-" ]; then t=$(cat); echo "=== PROG" >> {self.progs}; '
                 f'printf "%s\\n" "$t" >> {self.progs}; '
                 f'if [ -f {self.reject} ] && printf "%s" "$t" | grep -q rsv_tgout; then echo "Error: cgroupv2" >&2; exit 1; fi; fi\n'
                 'exit 0\n')

    def cgroup(self, on: bool = True) -> None:
        d = self.cg / CGROUP
        if on:
            d.mkdir(parents=True, exist_ok=True)
        elif d.exists():
            d.rmdir()

    def run(self, *args: str, **env) -> subprocess.CompletedProcess:
        base = {"PATH": f"{self.bin}:/usr/bin:/bin", "RSV_CONF_DIR": str(self.etc), "RSV_STATE": str(self.state),
                "UNIT": str(self.unit), "SELF_PATH": str(self.self_path), "GW_STATUS": str(self.gw_status)}
        base.update({k: str(v) for k, v in env.items()})
        self.log.write_text("", encoding="utf-8")
        self.progs.write_text("", encoding="utf-8")
        return subprocess.run([SH, str(self.script), *args], capture_output=True, text=True, env=base, timeout=30)

    def rule_marks(self) -> list[str]:
        return re.findall(r"fwmark (\S+) lookup", self.rules.read_text(encoding="utf-8"))


@pytest.fixture()
def tg(tmp_path):
    g = _TgGw(tmp_path)
    r = g.up_with()
    assert r.returncode == 0, r.stdout + r.stderr
    return g


# ── --tg on ────────────────────────────────────────────────────────────────

def test_tg_on_marks_only_the_agents_sockets_to_telegram_and_routes_them_into_awgrsv(tg):
    tg.cgroup()
    r = tg.run("--tg", "on")
    assert r.returncode == 0 and "tg: on" in r.stdout, r.stdout + r.stderr
    prog = tg.prog()
    assert [n.strip() for n in _set(prog, "tg_nets4").split("elements = {", 1)[1].split("}")[0].split(",")] == TG_NETS
    assert _chain(prog, "rsv_tgout") == [
        "type route hook output priority mangle + 1; policy accept;",
        f'ip daddr @tg_nets4 socket cgroupv2 level 2 "{CGROUP}" meta mark set 0x53'], \
        "метка ставится не только сокетам процесса агента к набору Telegram"
    assert 'oifname "awgrsv" meta mark 0x53 masquerade' in _chain(prog, "rsv_tgpost")
    assert 'oifname "awgrsv" meta mark 0x53 tcp flags syn tcp option maxseg size set rt mtu' in _chain(prog, "rsv_tgmss"), \
        "нет клампа MSS на пути к Telegram через awgrsv"
    assert sorted(tg.rule_marks()) == ["0x52", "0x53"], tg.rules.read_text(encoding="utf-8")
    assert tg.kv()["tg"] == "on"


def test_tg_on_twice_adds_the_rule_once(tg):
    tg.cgroup()
    tg.run("--tg", "on")
    tg.run("--tg", "on")
    assert tg.rule_marks().count("0x53") == 1, tg.rules.read_text(encoding="utf-8")


def test_without_the_agents_cgroup_the_table_has_no_tg_block(tg):
    """Путь cgroup nft переводит в id при загрузке: юнита агента нет — блок не
    рисуется вовсе, иначе таблица отвергнута целиком."""
    r = tg.run("--tg", "on")
    assert r.returncode == 0, r.stdout + r.stderr
    prog = tg.prog()
    assert "table inet awg_rsv_guard {" in prog and "rsv_tgout" not in prog and "tg_nets4" not in prog, prog
    assert tg.kv()["tg"] == "on", "состояние обхода не записано — после появления cgroup блок не встанет"


def test_a_table_refused_with_the_tg_block_is_redrawn_without_it(tg):
    tg.cgroup()
    tg.reject.write_text("")
    r = tg.run("--tg", "on")
    assert r.returncode == 0, f"отказ блока tg уронил обвязку линка: {r.stdout + r.stderr}"
    assert "обход Telegram снят" in r.stdout, r.stdout
    assert "rsv_tgout" not in tg.prog() and "table inet awg_rsv_guard {" in tg.prog(), "таблица без блока не нарисована"
    assert tg.kv()["tg"] == "off"
    assert tg.rule_marks() == ["0x52"], "правило метки обхода осталось без метки"


def test_tg_on_with_the_link_down_only_remembers_and_the_next_up_draws_the_block(tmp_path):
    g = _TgGw(tmp_path)
    g.cgroup()
    r = g.run("--tg", "on")
    assert r.returncode == 0 and not g.prog(), "таблица нарисована без линка"
    assert g.kv()["tg"] == "on"
    g.up_with()
    assert "rsv_tgout" in g.prog(), "состояние tg=1 не дошло до таблицы при подъёме линка"
    assert "0x53" in g.rule_marks()


def test_tg_state_survives_open_and_close(tg):
    tg.cgroup()
    tg.run("--tg", "on")
    tg.run("--open", "10.9.1.0/24")
    tg.run("--close")
    assert tg.kv()["tg"] == "on", "открытие и закрытие пересылки сбросили обход"
    tg.run("--reassert")
    assert "rsv_tgout" in tg.prog() and "0x53" in tg.rule_marks(), "после --reassert (загрузка) обход не восстановлен"


# ── --tg off и прочее ───────────────────────────────────────────────────────

def test_tg_off_takes_the_block_and_its_rule_and_keeps_the_link_rule(tg):
    tg.cgroup()
    tg.run("--tg", "on")
    r = tg.run("--tg", "off")
    assert r.returncode == 0 and "tg: off" in r.stdout
    assert "rsv_tgout" not in tg.prog() and "table inet awg_rsv_guard {" in tg.prog()
    assert tg.rule_marks() == ["0x52"], tg.rules.read_text(encoding="utf-8")
    assert tg.kv()["tg"] == "off"


@pytest.mark.parametrize("arg", ["", "yes", "ON"])
def test_tg_with_a_wrong_word_refuses_and_changes_nothing(tg, arg):
    tg.cgroup()
    tg.run("--tg", "on")
    r = tg.run("--tg", *([arg] if arg else []))
    assert r.returncode == 1 and "tg: on|off" in r.stdout, r.stdout
    assert tg.kv()["tg"] == "on" and "0x53" in tg.rule_marks()


def test_tg_without_root_refuses_and_touches_nothing(tg):
    (tg.tmp / "notroot").write_text("")
    r = tg.run("--tg", "on")
    assert r.returncode == 1 and "нужен root" in r.stdout
    assert not [c for c in tg.calls() if c.startswith(("nft", "ip rule add"))], tg.calls()
    (tg.tmp / "notroot").unlink()
    assert tg.kv()["tg"] == "off" and "0x53" not in tg.rule_marks(), "отказ без root записал состояние обхода"


def test_down_takes_the_tg_rule_too(tg):
    tg.cgroup()
    tg.run("--tg", "on")
    tg.run("--down")
    assert tg.rule_marks() == [], tg.rules.read_text(encoding="utf-8")


def test_the_telegram_ranges_are_the_same_everywhere():
    """Набор Telegram — один: обвязка шлюза, линк к резерву, зона второго сервера и
    агент (метка сокетов) должны совпадать, иначе часть адресов Bot API идёт мимо обхода."""
    zone = _nets(ROOT / "install" / "reserve-zone-setup.sh")
    gw_setup = _nets(ROOT / "install" / "routing-gw-setup.sh")
    assert TG_NETS == zone == gw_setup, (TG_NETS, zone, gw_setup)
    assert _agent_ranges() == TG_NETS, "диапазоны метки агента разошлись с набором скриптов"


def _agent_ranges() -> list[str]:
    src = (ROOT / "awgbot" / "domain" / "gateway" / "tgmark.py").read_text(encoding="utf-8")
    body = src.split("TG_RANGES = (", 1)[1].split(")", 1)[0]
    return re.findall(r'"([0-9./]+)"', body)


# ── зона второго сервера: блок tg ───────────────────────────────────────────

LINK = "[Interface]\nPrivateKey = DUMMY\nAddress = {addr}\n"


@pytest.fixture()
def zone(tmp_path):
    z = _Zone(tmp_path)
    (z.d / "links").mkdir()
    return z


def _links(zone: _Zone, *addrs: str) -> None:
    for i, a in enumerate(addrs, 1):
        (zone.d / "links" / f"awglink{i}.conf").write_text(LINK.format(addr=a), encoding="utf-8")


def _zchain(text: str, name: str) -> list[str]:
    body = text.split(f"chain {name} {{", 1)[1].split("\n    }", 1)[0]
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def test_the_zone_lets_the_gateway_link_sides_out_only_to_telegram_443(zone):
    _links(zone, "10.99.97.1/30", "10.99.97.5/30")
    r = zone.sh('zone_nft ""')
    assert r.returncode == 0, r.stdout + r.stderr
    table = zone.tables()[-1][0]
    fwd = _zchain(table, "forward")
    src = "{ 10.99.97.2/32, 10.99.97.6/32 }"
    meter = (f'iifname "awglink*" ip saddr {src} ip daddr @tg_out4 tcp dport 443 ct state new '
             'meter tg_src { ip saddr ct count over 8 } drop')
    accept = f'iifname "awglink*" oifname "awggx-n" ip saddr {src} ip daddr @tg_out4 tcp dport 443 accept'
    assert meter in fwd and accept in fwd, f"нет пути шлюзовых сторон к Telegram: {fwd}"
    assert fwd.index(meter) < fwd.index(accept) < fwd.index('iifname "awglink*" drop'), fwd
    out = [ln for ln in fwd if 'oifname "awggx-n"' in ln]
    assert out == [accept], f"в veth выхода к Telegram пускается не только tcp 443 набора: {out}"
    assert _zchain(table, "zone_mss") == ["type filter hook postrouting priority mangle; policy accept;",
                                          'oifname "awggx-n" tcp flags syn tcp option maxseg size set rt mtu']
    tg_set = next(ln for ln in table.splitlines() if "set tg_out4" in ln)
    assert re.findall(r"[0-9.]+/[0-9]+", tg_set) == TG_NETS, tg_set


def test_without_gateway_links_the_zone_has_no_telegram_path(zone):
    r = zone.sh('zone_nft ""')
    assert r.returncode == 0, r.stdout + r.stderr
    table = zone.tables()[-1][0]
    assert "@tg_out4 tcp dport 443" not in table and "zone_mss" not in table, \
        "путь к Telegram без единого линка шлюза — с пустым набором источников"


def test_a_link_without_an_address_is_not_a_source(zone):
    _links(zone, "10.99.97.1/30")
    (zone.d / "links" / "awglink9.conf").write_text("[Interface]\nPrivateKey = DUMMY\n", encoding="utf-8")
    zone.sh('zone_nft ""')
    table = zone.tables()[-1][0]
    assert "ip saddr { 10.99.97.2/32 } ip daddr @tg_out4" in table, table


def _ip_without_veth(zone: _Zone) -> None:
    fake_cmd(zone.bin / "ip", (
        f'echo "ip $*" >> "{zone.log}"\n'
        'if [ "$1" = netns ] && [ "$2" = exec ]; then shift 3; exec "$@"; fi\n'
        'case "$*" in "link show awggx-"*) exit 1 ;; "link add awggx-h"*) exit 1 ;; "rule del"*) exit 1 ;; esac\n'
        'exit 0\n'))


def test_without_the_veth_the_zone_has_no_telegram_path_even_with_links(zone):
    _links(zone, "10.99.97.1/30")
    _ip_without_veth(zone)
    zone.sh('zone_nft ""')
    assert "@tg_out4 tcp dport 443" not in zone.tables()[-1][0]


def test_the_telegram_path_routes_each_range_via_the_host_and_each_source_by_rule(zone):
    _links(zone, "10.99.97.1/30", "10.99.97.5/30")
    r = zone.sh("tg_path_up")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = zone.calls()
    for n in TG_NETS:
        assert f"ip route replace {n} via 169.254.77.9 dev awggx-n table 153" in calls, f"нет маршрута {n} в таблице 153"
    rules = [c for c in calls if c.startswith("ip rule add")]
    assert rules == ["ip rule add from 10.99.97.2/32 lookup 153", "ip rule add from 10.99.97.6/32 lookup 153"], rules
    assert "ip addr replace 169.254.77.10/30 dev awggx-n" in calls
    st = zone.read_state()
    assert st.get("tg") == "up" and st.get("zone_tg_cidr") == "169.254.77.8/30", st


def test_the_telegram_veth_follows_zone_tg_cidr(zone):
    _links(zone, "10.99.97.1/30")
    zone.sh("tg_path_up", ZONE_TG_CIDR="169.254.77.12/30")
    calls = zone.calls()
    assert "ip addr replace 169.254.77.13/30 dev awggx-h" in calls and "ip addr replace 169.254.77.14/30 dev awggx-n" in calls
    assert zone.read_state().get("zone_tg_cidr") == "169.254.77.12/30"


def test_a_veth_that_does_not_come_up_leaves_the_zone_without_the_path_but_working(zone):
    _links(zone, "10.99.97.1/30")
    _ip_without_veth(zone)
    r = zone.sh("tg_path_up; echo after")
    assert r.returncode == 0 and "after" in r.stdout, r.stdout + r.stderr
    assert "выхода агентов в Telegram нет" in r.stdout
    assert not zone.state.exists() or zone.read_state().get("tg") != "up", "состояние говорит tg=up без veth"
    assert not [c for c in zone.calls() if "table 153" in c], "маршруты к Telegram без veth"
