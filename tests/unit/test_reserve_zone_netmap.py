"""NETMAP подсетей клонов основы в гостевой зоне резерва
(install/reserve-zone-setup.sh): план блоков пула `ZONE_POOL` (`netmap_plan`),
правила `snat ip prefix` в таблице зоны и откат на маскарад, если nft их не
принял (`zone_nft`), маршрут блока у хоста через veth (`--take`), режим CAKE
по наличию NETMAP (`cake_on`) и сброс `netmap` в состоянии при `--relay`,
`--teardown`, `--wipe`.

Функции скрипта подключаются целиком (без разбора режима в конце), root,
`ip`, `nft`, `tc`, `mount`, `systemctl` подменены в PATH; пространство зоны —
файл во временном каталоге. Подменный `ip netns exec` выполняет команду сам,
так что `nft` зоны тоже попадает в подмену: каждый `nft -f` оставляет текст
таблицы и снимок state.json на момент вызова.

Цена ошибки: два клона на одном блоке или блок шире пула — ответы
устройствам основы уходят не тем людям или не доходят вовсе; таблица,
отвергнутая старым ядром, без отката — `--take` падает, зона без таблицы,
пользователи основы без VPN весь переезд; `netmap`, оставшийся в состоянии
после снятия зоны, — хост маскарадит и маршрутизирует блоки, которых нет;
маршрута блока нет — ответы на адреса устройств основы хост уводит
маршрутом по умолчанию в интернет; CAKE в `triple-isolate` без NETMAP —
вся зона одним источником делит канал поровну с каждым пользователем
резерва, т. е. все пользователи основы вместе получают долю одного.
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
CONF = "[Interface]\nPrivateKey = DUMMY\nListenPort = {port}\nAddress = {addr}\nMTU = 1380\n"


def _functions() -> str:
    """Скрипт без разбора режима в конце: только определения и переменные."""
    src = SCRIPT.read_text(encoding="utf-8")
    return src[:src.index('\ncase "${1:-}" in')] + "\n"


class _Zone:
    """Хост для прогона функций скрипта: подмены команд пишут журнал вызовов,
    `nft -f` сохраняет таблицу и снимок состояния, `reject_prefix()` заставляет
    nft отвергать таблицу с `snat ip prefix` (ядро ниже 5.8)."""

    def __init__(self, tmp_path: Path):
        self.d = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.log = tmp_path / "calls.log"
        self.state_dir = tmp_path / "state"
        self.state = self.state_dir / "state.json"
        self.run_dir = tmp_path / "run"
        self.ns = tmp_path / "netns-zone"
        self.ns.write_text("")
        # python для состояния — интерпретатор прогона, как venv бота
        venv = tmp_path / "inst" / "venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").symlink_to(sys.executable)
        fake_cmd(self.bin / "id", 'case "$1" in -u) echo 0 ;; *) echo "uid=0" ;; esac\n')
        fake_cmd(self.bin / "uname", 'case "$1" in -r) echo "6.12.47" ;; *) echo Linux ;; esac\n')
        fake_cmd(self.bin / "modinfo", "exit 0\n")
        fake_cmd(self.bin / "ip", (
            f'echo "ip $*" >> "{self.log}"\n'
            'if [ "$1" = netns ] && [ "$2" = exec ]; then shift 3; exec "$@"; fi\n'
            'case "$*" in "-j route show default") echo \'[{"dev": "eth0"}]\' ;;\n'
            f'  "-4 route show") cat "{tmp_path}/routes" 2>/dev/null ;;\n'
            # правила нет — guest_stop снимает его в цикле до первого отказа
            '  "rule del"*) exit 1 ;; esac\n'
            'exit 0\n'))
        fake_cmd(self.bin / "nft", (
            'if [ "$1" = "-f" ]; then\n'
            f'  n=$(ls "{tmp_path}" | grep -c "^nft\\.[0-9]*\\.txt$"); n=$((n + 1))\n'
            f'  cat > "{tmp_path}/nft.$n.txt"\n'
            f'  cp "{self.state}" "{tmp_path}/nft.$n.state" 2>/dev/null || : > "{tmp_path}/nft.$n.state"\n'
            f'  echo "nft -f #$n" >> "{self.log}"\n'
            f'  if [ -f "{tmp_path}/nft.reject" ] && grep -q "snat ip prefix" "{tmp_path}/nft.$n.txt"; then\n'
            '    echo "Error: syntax error, unexpected prefix" >&2; exit 1; fi\n'
            '  exit 0\n'
            'fi\n'
            f'echo "nft $*" >> "{self.log}"\n'))
        fake_cmd(self.bin / "tc", (
            f'echo "tc $*" >> "{self.log}"\n'
            'case "$*" in "qdisc show"*) echo "qdisc fq_codel 0: root refcnt 2" ;; esac\n'
            'exit 0\n'))
        fake_cmd(self.bin / "mountpoint", "exit 1\n")
        for tool in ("mount", "umount", "systemctl", "sysctl", "awg", "conntrack"):
            fake_cmd(self.bin / tool, f'echo "{tool} $*" >> "{self.log}"\nexit 0\n')

    def routes(self, text: str) -> None:
        """Таблица main хоста — вывод `ip -4 route show`."""
        (self.d / "routes").write_text(text, encoding="utf-8")

    def reject_prefix(self) -> None:
        (self.d / "nft.reject").write_text("")

    def put_state(self, **data: str) -> None:
        self.state_dir.mkdir(exist_ok=True)
        self.state.write_text(json.dumps(data), encoding="utf-8")

    def read_state(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def tables(self) -> list[tuple[str, dict]]:
        """Каждый `nft -f` по порядку: текст таблицы и состояние в момент вызова."""
        out, n = [], 1
        while (self.d / f"nft.{n}.txt").exists():
            st = (self.d / f"nft.{n}.state").read_text(encoding="utf-8")
            out.append(((self.d / f"nft.{n}.txt").read_text(encoding="utf-8"), json.loads(st) if st else {}))
            n += 1
        return out

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def sh(self, body: str, *, stdin: str = "", **env: str) -> subprocess.CompletedProcess:
        full = {"PATH": f"{self.bin}:/usr/bin:/bin", "RSV_ZONE": "awgtst-зона", "RSV_RUN": str(self.run_dir),
                "RSV_VAR": str(self.d / "var"), "RSV_STATE_DIR": str(self.state_dir),
                "RSV_LINKS_DIR": str(self.d / "links"), "INSTALL_DIR": str(self.d / "inst"),
                "HOME": str(self.d), "TMPDIR": os.environ.get("TMPDIR", "/tmp")}
        full.update(env)
        prog = (_functions() + f'NS="{self.ns}"; UNIT="{self.d}/guest.service"; DNS_UNIT="{self.d}/dns.service"; '
                f'JOURNAL_CONF="{self.d}/journald.conf"\n' + body + "\n")
        return subprocess.run(["bash", "-c", prog], input=stdin, capture_output=True, text=True, timeout=60, env=full)


@pytest.fixture()
def zone(tmp_path):
    return _Zone(tmp_path)


def _plan(zone: _Zone, *cidrs: str, pool: str = "169.254.96.0/19", busy: str = "") -> dict[str, str]:
    r = zone.sh(f'netmap_plan "{busy}" ' + " ".join(cidrs), ZONE_POOL=pool)
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip()
    pairs = dict(p.split("=", 1) for p in out.split()) if out else {}
    assert len(pairs) == len(out.split()), f"подсеть дважды в плане: {out!r}"
    return pairs


# ── план блоков ──────────────────────────────────────────────────────────────

def test_one_clone_subnet_gets_the_first_block_of_the_pool_of_the_same_length(zone):
    """Адрес клона (как в Address конфига, с адресом хоста) — подсеть целиком
    на первый блок пула той же длины: устройство .N основы выходит адресом .N блока."""
    assert _plan(zone, "10.8.1.1/24") == {"10.8.1.0/24": "169.254.96.0/24"}


def test_two_clone_subnets_get_two_different_blocks(zone):
    got = _plan(zone, "10.8.1.1/24", "10.8.2.1/24")
    assert got == {"10.8.1.0/24": "169.254.96.0/24", "10.8.2.0/24": "169.254.97.0/24"}, \
        "два клона на одном блоке — ответы устройствам второго уходят устройствам первого"


def test_blocks_of_different_lengths_do_not_overlap(zone):
    """Блок меньшей подсети занят — блок большей идёт за ним, а не поверх."""
    got = _plan(zone, "10.8.1.1/25", "10.8.2.1/24")
    import ipaddress
    a, b = (ipaddress.ip_network(got[k]) for k in ("10.8.1.0/25", "10.8.2.0/24"))
    assert a.prefixlen == 25 and b.prefixlen == 24, f"длина блока не та же, что у подсети: {got}"
    assert not a.overlaps(b), f"блоки пересекаются: {got}"


def test_a_repeated_subnet_is_planned_once(zone):
    """Тот же адрес (или та же подсеть другим адресом) второй раз — повтор:
    второе отображение одной подсети на другой блок nft принял бы, но
    ответы ушли бы по первому, а блок пропал бы зря."""
    assert _plan(zone, "10.8.1.1/24", "10.8.1.7/24") == {"10.8.1.0/24": "169.254.96.0/24"}
    assert _plan(zone, "10.8.1.1/24", "10.8.1.1/25") == {"10.8.1.0/24": "169.254.96.0/24"}, \
        "подсеть внутри уже отображённой получила свой блок — два правила на одни адреса"


def test_a_subnet_wider_than_the_pool_stays_under_the_masquerade(zone):
    assert _plan(zone, "10.8.0.1/16") == {}, "подсеть шире пула отображена — адреса устройств за пределами пула"
    assert _plan(zone, "10.8.0.1/16", "10.9.1.1/24") == {"10.9.1.0/24": "169.254.96.0/24"}, \
        "широкая подсеть сорвала план остальных клонов"


def test_when_the_pool_runs_out_the_rest_stay_under_the_masquerade(zone):
    got = _plan(zone, "10.8.1.1/24", "10.8.2.1/24", "10.8.3.1/24", pool="169.254.96.0/23")
    assert got == {"10.8.1.0/24": "169.254.96.0/24", "10.8.2.0/24": "169.254.97.0/24"}, \
        f"третий клон без места в пуле получил блок: {got}"


def test_no_clones_give_an_empty_plan(zone):
    assert _plan(zone) == {}


def test_the_plan_follows_the_pool_from_the_environment(zone):
    """Пул — ключ reserve.zone_pool, обёртка передаёт его окружением; скрипт
    со своим умолчанием разошёлся бы с маскарадом блоков у хоста."""
    assert _plan(zone, "10.8.1.1/24", pool="100.127.0.0/20") == {"10.8.1.0/24": "100.127.0.0/24"}


def test_without_the_environment_the_script_takes_the_template_pool(zone):
    """Ручной запуск скрипта (без обёртки) — пул по умолчанию тот же, что в шаблоне conf/app.yaml."""
    r = zone.sh('netmap_plan "" 10.8.1.1/24')
    assert r.stdout.split() == ["10.8.1.0/24=169.254.96.0/24"], r.stdout + r.stderr


# ── занятое у хоста ──────────────────────────────────────────────────────────

def test_a_busy_block_is_skipped_for_the_next_free_one(zone):
    """`ip route replace` на блок, совпавший с маршрутом хоста, молча
    перебил бы его: сеть админа за этим маршрутом пропала бы."""
    assert _plan(zone, "10.8.1.1/24", busy="169.254.96.0/24") == {"10.8.1.0/24": "169.254.97.0/24"}
    assert _plan(zone, "10.8.1.1/24", "10.8.2.1/24", busy="169.254.96.10 169.254.97.0/25") == \
        {"10.8.1.0/24": "169.254.98.0/24", "10.8.2.0/24": "169.254.99.0/24"}, "блок задевает занятое частично"


def test_garbage_in_the_busy_list_does_not_break_the_plan(zone):
    assert _plan(zone, "10.8.1.1/24", busy="мусор 169.254.96.0/24 dev") == {"10.8.1.0/24": "169.254.97.0/24"}


def test_a_fully_busy_pool_leaves_the_clones_under_the_masquerade(zone):
    busy = " ".join(f"169.254.{96 + i}.0/24" for i in range(32))
    assert _plan(zone, "10.8.1.1/24", busy=busy) == {}


def test_a_route_wider_than_the_block_does_not_take_the_pool(zone):
    """Маршрут шире блока (link-local /16 у avahi и NetworkManager, сам пул
    одним маршрутом) проигрывает блоку по длине префикса — ему блок не мешает."""
    assert _plan(zone, "10.8.1.1/24", busy="169.254.0.0/16") == {"10.8.1.0/24": "169.254.96.0/24"}
    assert _plan(zone, "10.8.1.1/24", busy="169.254.96.0/19") == {"10.8.1.0/24": "169.254.96.0/24"}


def test_a_broken_clone_address_drops_netmap_only_for_that_clone(zone):
    assert _plan(zone, "10.8.1.1/24", "битый", "10.8.2.1/24") == \
        {"10.8.1.0/24": "169.254.96.0/24", "10.8.2.0/24": "169.254.97.0/24"}


def test_the_busy_list_is_the_zone_veths_and_the_host_routes_except_default_and_own_blocks(zone):
    zone.routes("default via 203.0.113.1 dev eth0 proto dhcp src 203.0.113.10 metric 100\n"
                "169.254.96.0/24 dev ens4 proto kernel scope link src 169.254.96.1\n"
                "10.8.1.0/24 dev awg0 proto kernel scope link src 10.8.1.1\n"
                "169.254.98.0/24 via 169.254.77.2 dev awggv-h\n"
                "169.254.77.0/30 dev awggv-h proto kernel scope link src 169.254.77.1\n")
    r = zone.sh("netmap_busy", ZONE_CIDR="169.254.77.0/30", ZONE_RF_CIDR="169.254.77.4/30")
    assert r.returncode == 0, r.stderr
    got = r.stdout.split()
    assert got[:2] == ["169.254.77.0/30", "169.254.77.4/30"], "подсети veth зоны не считаются занятыми"
    assert "169.254.96.0/24" in got and "10.8.1.0/24" in got, f"маршруты хоста не попали в занятое: {got}"
    assert "default" not in got, "маршрут по умолчанию считается занятым — пул пуст всегда"
    assert "169.254.98.0/24" not in got, "блок прошлого --take (на awggv-h) занят — повтор сдвигает адреса"


def test_take_avoids_a_block_the_host_already_routes(zone):
    zone.routes("default via 203.0.113.1 dev eth0\n169.254.96.0/24 dev ens4 proto kernel scope link\n")
    r = _take(zone, extra=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert zone.read_state()["netmap"] == "10.8.1.0/24=169.254.97.0/24"
    assert [c for c in zone.calls() if c.startswith("ip route replace 169.254.9")] == \
        ["ip route replace 169.254.97.0/24 via 169.254.77.2 dev awggv-h"], "маршрут хоста на 169.254.96.0/24 перебит"


# ── таблица зоны ─────────────────────────────────────────────────────────────

def _post(table: str) -> list[str]:
    """Строки цепочки zone_post."""
    body = table.split("chain zone_post {", 1)[1].split("}\n}", 1)[0]
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def test_the_zone_table_maps_each_clone_subnet_to_its_block_before_the_masquerade(zone):
    zone.put_state(netmap="10.8.1.0/24=169.254.96.0/24 10.8.2.0/24=169.254.97.0/24")
    r = zone.sh('zone_nft "awg0 awg1"')
    assert r.returncode == 0, r.stdout + r.stderr
    (table, _), = zone.tables()
    post = _post(table)
    want = ['oifname "awggv-n" ip saddr 10.8.1.0/24 snat ip prefix to ip saddr map { 10.8.1.0/24 : 169.254.96.0/24 }',
            'oifname "awggv-n" ip saddr 10.8.2.0/24 snat ip prefix to ip saddr map { 10.8.2.0/24 : 169.254.97.0/24 }']
    assert post[1:3] == want, "\n".join(post)
    assert post[-1] == 'oifname { "awggv-n", "awggr-n" } masquerade', \
        "маскарад прочего (бот основы, резолвер, РФ-veth) пропал или встал раньше NETMAP"
    assert "ВНИМАНИЕ" not in r.stdout
    assert zone.read_state()["netmap"].split() == ["10.8.1.0/24=169.254.96.0/24", "10.8.2.0/24=169.254.97.0/24"]


def test_a_zone_without_netmap_masquerades_everything(zone):
    zone.put_state(netmap="")
    assert zone.sh('zone_nft "awg0"').returncode == 0
    (table, _), = zone.tables()
    assert "snat" not in table and _post(table)[-1] == 'oifname { "awggv-n", "awggr-n" } masquerade'


def test_a_shell_without_clones_draws_no_netmap_even_if_the_state_has_one(zone):
    """Таблица без клонов (оболочка, снятие) — правил NETMAP нет: подсетей клонов в зоне нет."""
    zone.put_state(netmap="10.8.1.0/24=169.254.96.0/24")
    assert zone.sh('zone_nft ""').returncode == 0
    (table, _), = zone.tables()
    assert "snat" not in table


def test_a_kernel_that_rejects_netmap_gets_the_masquerade_a_warning_and_an_empty_netmap(zone):
    zone.put_state(netmap="10.8.1.0/24=169.254.96.0/24")
    zone.reject_prefix()
    r = zone.sh('zone_nft "awg0"')
    assert r.returncode == 0, f"таблица с маскарадом не встала после отказа NETMAP: {r.stdout}{r.stderr}"
    tables = zone.tables()
    assert len(tables) == 2, f"таблица загружалась {len(tables)} раз(а), ждали второй заход без NETMAP"
    assert "snat ip prefix" in tables[0][0] and "snat" not in tables[1][0], "второй заход снова с NETMAP"
    assert _post(tables[1][0])[-1] == 'oifname { "awggv-n", "awggr-n" } masquerade'
    assert any(ln.startswith("ВНИМАНИЕ") and "NETMAP" in ln for ln in r.stdout.splitlines()), r.stdout
    assert zone.read_state()["netmap"] == "", \
        "netmap остался в состоянии — хост проложил бы маршруты и маскарад блоков, которых в зоне нет"


def test_a_table_rejected_for_another_reason_fails_and_keeps_the_netmap(zone):
    """nft отверг и таблицу без NETMAP — отказ режима, как прежде; состояние не трогается."""
    zone.put_state(netmap="10.8.1.0/24=169.254.96.0/24")
    fake_cmd(zone.bin / "nft", f'if [ "$1" = "-f" ]; then cat > /dev/null; echo "nft -f" >> "{zone.log}"; exit 1; fi\n')
    r = zone.sh('zone_nft "awg0" && echo LOADED')
    assert "LOADED" not in r.stdout, "отвергнутая таблица сочтена загруженной"
    assert [c for c in zone.calls() if c == "nft -f"] == ["nft -f", "nft -f"]
    assert "ВНИМАНИЕ" not in r.stdout
    assert zone.read_state()["netmap"] == "10.8.1.0/24=169.254.96.0/24"


def test_a_table_without_netmap_rejected_is_not_retried(zone):
    zone.put_state(netmap="")
    fake_cmd(zone.bin / "nft", f'if [ "$1" = "-f" ]; then cat > /dev/null; echo "nft -f" >> "{zone.log}"; exit 1; fi\n')
    r = zone.sh('zone_nft "awg0" && echo LOADED')
    assert "LOADED" not in r.stdout
    assert [c for c in zone.calls() if c == "nft -f"] == ["nft -f"], "повтор без NETMAP, когда NETMAP и не было"


# ── --take ───────────────────────────────────────────────────────────────────

def _take(zone: _Zone, *, extra: bool = True) -> subprocess.CompletedProcess:
    env = {"GUEST_IF": "awg0", "GUEST_ADDR": "10.8.1.1/24", "GUEST_DNS": "", "GUEST_UPSTREAMS": "", "GUEST_ADMIN": ""}
    if extra:
        (zone.run_dir / "awg").mkdir(parents=True, exist_ok=True)
        (zone.run_dir / "awg" / "awg1.conf").write_text(CONF.format(port=45878, addr="10.8.2.1/24"))
        env["GUEST_EXTRA"] = "awg1=10.8.2.1/24"
    return zone.sh("cmd_take", stdin=CONF.format(port=45877, addr="10.8.1.1/24"), **env)


def test_take_writes_the_netmap_before_the_zone_table_and_routes_each_block_into_the_zone(zone):
    r = _take(zone)
    assert r.returncode == 0 and "take: ok" in r.stdout, r.stdout + r.stderr
    want = "10.8.1.0/24=169.254.96.0/24 10.8.2.0/24=169.254.97.0/24"
    table, at_load = zone.tables()[0]
    assert at_load.get("netmap") == want, f"таблица зоны рисовалась без плана в состоянии: {at_load}"
    assert "map { 10.8.2.0/24 : 169.254.97.0/24 }" in table, "второй клон (окно переезда) без NETMAP"
    routes = [c for c in zone.calls() if c.startswith("ip route replace 169.254.")]
    assert routes == ["ip route replace 169.254.96.0/24 via 169.254.77.2 dev awggv-h",
                      "ip route replace 169.254.97.0/24 via 169.254.77.2 dev awggv-h"], \
        f"ответы на блоки хост не ведёт в зону: {routes}"
    st = zone.read_state()
    assert st["netmap"] == want and st["phase"] == "taking"


def test_take_on_a_kernel_without_netmap_routes_no_blocks(zone):
    """NETMAP не встал — блоков в зоне нет; маршрут на них увёл бы в зону
    чужие адреса без правила, которое их ждёт."""
    zone.reject_prefix()
    r = _take(zone, extra=False)
    assert r.returncode == 0 and "take: ok" in r.stdout, r.stdout + r.stderr
    assert "ВНИМАНИЕ: nft не принял NETMAP" in r.stdout
    assert not [c for c in zone.calls() if c.startswith("ip route replace 169.254.9")], zone.calls()
    assert zone.read_state()["netmap"] == ""


def test_a_repeated_take_gives_the_same_blocks(zone):
    """Повтор --take (перезапуск переезда) — те же блоки: устройства основы
    не меняют адрес наружу на ходу, соединения не рвутся."""
    assert _take(zone).returncode == 0
    first = zone.read_state()["netmap"]
    # маршруты блоков прошлого захода стоят на awggv-h — их скрипт не считает чужими
    zone.routes("default via 203.0.113.1 dev eth0\n" + "".join(
        f"{p.split('=')[1]} via 169.254.77.2 dev awggv-h\n" for p in first.split()))
    assert _take(zone).returncode == 0
    assert zone.read_state()["netmap"] == first
    routes = [c for c in zone.calls() if c.startswith("ip route replace 169.254.9")]
    assert len(routes) == 4 and len(set(routes)) == 2, f"маршруты не заменяются, а копятся: {routes}"


# ── CAKE ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("netmap, mode", [("10.8.1.0/24=169.254.96.0/24", "triple-isolate"), ("", "dual-dsthost")],
                         ids=["netmap", "masquerade"])
def test_cake_isolates_by_device_only_when_the_devices_are_visible(zone, netmap, mode):
    zone.put_state(netmap=netmap)
    r = zone.sh("cake_on")
    assert r.returncode == 0, r.stderr
    cake = [c for c in zone.calls() if c.startswith("tc qdisc replace")]
    assert cake == [f"tc qdisc replace dev eth0 root cake besteffort {mode} nat"], cake
    st = zone.read_state()
    assert (st["cake"], st["cake_dev"], st["cake_prev"]) == ("on", "eth0", "fq_codel")


# ── снятие ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["cmd_relay", "cmd_teardown", "cmd_wipe"])
def test_leaving_the_zone_clears_the_netmap(zone, mode):
    """После снятия клонов блоков в зоне нет: netmap в состоянии — маскарад
    блоков у хоста по пустому месту и неверный режим CAKE при следующем
    переезде до --take."""
    zone.put_state(phase="serving", iface="awg0", ifaces="awg0", netmap="10.8.1.0/24=169.254.96.0/24",
                   relay_to="", port="45877", ports="45877")
    r = zone.sh(mode, RELAY_TO="203.0.113.5")
    assert r.returncode == 0, r.stdout + r.stderr
    st = zone.read_state()
    assert st.get("netmap", "") == "", f"{mode}: netmap остался {st.get('netmap')!r}"
    assert all("snat" not in t for t, _ in zone.tables()), "таблица без клонов нарисована с NETMAP"


def test_status_prints_the_netmap(zone):
    zone.put_state(phase="serving", netmap="10.8.1.0/24=169.254.96.0/24")
    lines = zone.sh("cmd_status").stdout.splitlines()
    assert "netmap=10.8.1.0/24=169.254.96.0/24" in lines, lines
