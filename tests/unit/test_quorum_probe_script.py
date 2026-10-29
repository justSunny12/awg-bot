"""Скрипт зондов кворума резерва install/quorum-probe-setup.sh (концепт «кворум»
§3.1): пространство `awg-quo`, интерфейсы зондов `awgq<линия><цель>`, таблица
`inet awg_quo`; режимы --up/--down/--wipe/--status.

Функции скрипта подключаются целиком (без разбора режима в конце) и гоняются
под `sh`; `id`, `ip`, `awg`, `nft`, `modprobe`, `sysctl` подменены в PATH.
Подменный `ip` ведёт список интерфейсов файлами (созданный у хоста, перенесённый
в пространство, переименованный), `ip netns exec` выполняет команду сам — так
`nft -f` пространства попадает в подмену и оставляет текст таблицы. Файл
пространства — во временном каталоге (`NSP`).

Цена ошибки: в таблице `awg_quo` выход не только на ответчика или маршрут по
умолчанию в пространстве — зонд голосующего становится выходом в интернет и в
подсеть пользователей мимо файервола; конфиг с хуками (`PostUp`), вторым пиром
или `AllowedIPs` шире /32 — root шлюза выполняет чужую команду или зонд
маршрутизирует больше, чем ответчика; интерфейс не с префиксом `awgq` — прежний
поиск аплинка и `--wipe` его не узнают; `--status` не в виде key=value — бот
не видит рукопожатий зондов.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import fake_cmd

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "install" / "quorum-probe-setup.sh"
PRIV = "DUMMY" + "A" * 38 + "="
PUB = "DUMMY" + "B" * 38 + "="
PSK = "DUMMY" + "C" * 38 + "="


def probe_conf(*, addr: str = "169.254.88.5/32", resp: str = "169.254.88.1/32", extra_if: str = "",
               extra_peer: str = "", peers: int = 1, endpoint: bool = True) -> str:
    """Конфиг зонда, как его собирает сервер (reserve_probe_conf)."""
    head = f"[Interface]\nAddress = {addr}\nPrivateKey = {PRIV}\nJc = 5\nH1 = 100-200\n{extra_if}"
    peer = (f"\n[Peer]\nPublicKey = {PUB}\nPresharedKey = {PSK}\nAllowedIPs = {resp}\n"
            + ("Endpoint = 203.0.113.10:43125\n" if endpoint else "") + extra_peer)
    return head + peer * peers


def _functions() -> str:
    src = SCRIPT.read_text(encoding="utf-8")
    return src[:src.index('\ncase "${1:-}" in')] + "\n"


class _Host:
    """Хост голосующего моделью файлов: `links/<имя>` — интерфейс (у хоста или в
    пространстве — видно по журналу), `netns/awg-quo` — пространство, `nft.N.txt`
    — каждый текст таблицы, `setconf.<имя>` — что ушло в awg setconf/syncconf."""

    def __init__(self, tmp_path: Path):
        self.d = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        (tmp_path / "links").mkdir()
        (tmp_path / "netns").mkdir()
        self.conf_dir = tmp_path / "quorum"
        self.log = tmp_path / "calls.log"
        d = tmp_path
        fake_cmd(self.bin / "id", 'case "$1" in -u) echo "${FAKE_UID:-0}" ;; *) echo "uid=0" ;; esac\n')
        fake_cmd(self.bin / "ip", (
            f'echo "ip $*" >> "{self.log}"\n'
            'if [ "$1" = netns ] && [ "$2" = exec ]; then shift 3; exec "$@"; fi\n'
            f'if [ "$1" = netns ] && [ "$2" = add ]; then : > "{d}/netns/$3"; exit 0; fi\n'
            f'if [ "$1" = netns ] && [ "$2" = del ]; then rm -f "{d}/netns/$3"; exit 0; fi\n'
            f'if [ "$1" = -o ] && [ "$2" = link ]; then i=1; for f in "{d}"/links/*; do [ -f "$f" ] || continue; '
            'echo "$i: $(basename "$f"): <POINTOPOINT,NOARP,UP> mtu 1420"; i=$((i+1)); done; exit 0; fi\n'
            'case "$1 $2" in\n'
            f'  "link show") [ -f "{d}/links/$3" ]; exit $? ;;\n'
            f'  "link add") [ -f "{d}/fail_link_add" ] && exit 2; : > "{d}/links/$3"; exit 0 ;;\n'
            f'  "link del") rm -f "{d}/links/$3"; exit 0 ;;\n'
            'esac\n'
            f'if [ "$1 $2" = "link set" ] && [ "$4" = name ]; then mv "{d}/links/$3" "{d}/links/$5"; exit 0; fi\n'
            'exit 0\n'))
        fake_cmd(self.bin / "awg", (
            f'echo "awg $*" >> "{self.log}"\n'
            'case "$1" in\n'
            f'  setconf) cp "$3" "{d}/setconf.$2"; exit 0 ;;\n'
            f'  syncconf) [ -f "{d}/fail_syncconf" ] && exit 1; cp "$3" "{d}/setconf.$2"; exit 0 ;;\n'
            f'  show) cat "{d}/hs.$2" 2>/dev/null; exit 0 ;;\n'
            'esac\nexit 0\n'))
        fake_cmd(self.bin / "nft", (
            'if [ "$1" = "-f" ]; then\n'
            f'  n=$(ls "{d}" | grep -c "^nft\\.[0-9]*\\.txt$"); n=$((n + 1)); cat > "{d}/nft.$n.txt"\n'
            f'  echo "nft -f #$n" >> "{self.log}"; exit 0\nfi\nexit 0\n'))
        for tool in ("modprobe", "sysctl"):
            fake_cmd(self.bin / tool, "exit 0\n")

    def sh(self, body: str, **env: str) -> subprocess.CompletedProcess:
        full = {"PATH": f"{self.bin}:/usr/bin:/bin", "QUO_CONF_DIR": str(self.conf_dir), "HOME": str(self.d)}
        full.update(env)
        prog = _functions() + f'NSP="{self.d}/netns/awg-quo"\n' + body + "\n"
        return subprocess.run(["sh", "-c", prog], capture_output=True, text=True, timeout=60, env=full)

    def put(self, name: str, text: str) -> Path:
        p = self.d / name
        p.write_text(text, encoding="utf-8")
        return p

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def tables(self) -> list[str]:
        out, n = [], 1
        while (self.d / f"nft.{n}.txt").exists():
            out.append((self.d / f"nft.{n}.txt").read_text(encoding="utf-8"))
            n += 1
        return out

    def links(self) -> list[str]:
        return sorted(p.name for p in (self.d / "links").iterdir())


@pytest.fixture()
def host(tmp_path):
    return _Host(tmp_path)


def _chain(text: str, name: str) -> list[str]:
    body = text.split(f"chain {name} {{", 1)[1].split("\n    }", 1)[0]
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def test_the_script_parses_under_posix_sh():
    for shell in ("sh", "dash"):
        try:
            r = subprocess.run([shell, "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            continue
        assert r.returncode == 0, f"{shell} -n: {r.stderr}"


# ── проверка конфига ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [probe_conf(), probe_conf(extra_if="MTU = 1380\n"),
                                  probe_conf(resp="169.254.88.1/32 # ответчик")], ids=["норма", "MTU", "комментарий"])
def test_a_proper_probe_conf_passes_the_check(host, text):
    f = host.put("ok.conf", text)
    assert host.sh(f'conf_check "{f}"').returncode == 0, "годный конфиг зонда отвергнут — голос не поднимется"


BAD_CONFS = [
    (probe_conf(extra_if="PostUp = /bin/sh -c id\n"), "хук PostUp"),
    (probe_conf(extra_if="PreUp = id\n"), "хук PreUp"),
    (probe_conf(extra_if="Table = 51\n"), "Table"),
    (probe_conf(extra_if="DNS = 1.1.1.1\n"), "DNS"),
    (probe_conf(extra_if="SaveConfig = true\n"), "SaveConfig"),
    (probe_conf(peers=2), "второй пир"),
    (probe_conf(peers=0), "нет пира"),
    (probe_conf(resp="169.254.88.1/32, 10.8.1.0/24"), "AllowedIPs двумя значениями"),
    (probe_conf(resp="169.254.88.0/26"), "AllowedIPs шире /32"),
    (probe_conf(resp="0.0.0.0/0"), "маршрут по умолчанию"),
    (probe_conf(addr="169.254.88.5/26"), "адрес зонда не /32"),
    (probe_conf(endpoint=False), "нет Endpoint"),
    (probe_conf().replace(f"PrivateKey = {PRIV}\n", ""), "нет закрытого ключа"),
]


@pytest.mark.parametrize("text, why", BAD_CONFS, ids=[w for _, w in BAD_CONFS])
def test_a_probe_conf_with_anything_extra_is_refused(host, text, why):
    f = host.put("bad.conf", text)
    assert host.sh(f'conf_check "{f}"').returncode != 0, f"конфиг зонда принят: {why}"


def test_a_missing_conf_file_is_refused(host):
    assert host.sh(f'conf_check "{host.d}/нет.conf"').returncode != 0


# ── --up ─────────────────────────────────────────────────────────────────────

def _up(host, name="awgqah", text=None, **env):
    f = host.put("in.conf", text or probe_conf())
    return host.sh(f'cmd_up "{f}"', QUO_IF=name, **env)


def test_up_creates_the_interface_at_the_host_then_moves_it_into_the_namespace(host):
    """Интерфейс создаётся у хоста (сокет остаётся у хоста) и только потом
    переносится в awg-quo; адрес, маршрут до ответчика и подъём — уже там."""
    r = _up(host)
    assert r.returncode == 0 and "up: ok" in r.stdout, r.stdout + r.stderr
    calls = host.calls()
    add = next(i for i, c in enumerate(calls) if c.startswith("ip link add ") and "type amneziawg" in c)
    setconf = next(i for i, c in enumerate(calls) if c.startswith("awg setconf "))
    move = next(i for i, c in enumerate(calls) if " netns awg-quo" in c and c.startswith("ip link set "))
    assert "netns exec" not in calls[add], "интерфейс зонда создан сразу в пространстве — сокет не у хоста"
    assert add < setconf < move, f"порядок создание → ключи → перенос нарушен: {calls}"
    in_ns = calls[move + 1:]
    assert any(c == "ip netns exec awg-quo ip addr replace 169.254.88.5/32 dev awgqah" for c in in_ns), in_ns
    assert any(c == "ip netns exec awg-quo ip route replace 169.254.88.1/32 dev awgqah" for c in in_ns), in_ns
    assert any(c == "ip netns exec awg-quo ip link set awgqah up" for c in in_ns), in_ns
    assert host.links() == ["awgqah"]


def test_up_never_adds_a_default_route_in_the_namespace(host):
    """Выхода в интернет у зонда нет по построению: маршрут только /32 до ответчика."""
    assert _up(host).returncode == 0
    routes = [c for c in host.calls() if " route " in c]
    assert routes and all("169.254.88.1/32" in c for c in routes), f"маршрут шире ответчика: {routes}"
    # путь временного каталога содержит имя теста — смотрим только сами команды ip
    ip_cmds = [c for c in host.calls() if c.startswith(("ip ", "ip netns exec"))]
    assert not any(" default" in c or "0.0.0.0/0" in c for c in ip_cmds), "маршрут по умолчанию у зонда"


def test_setconf_gets_the_conf_without_awg_quick_extensions(host):
    """awg setconf не знает Address/MTU — с ними интерфейс не поднялся бы."""
    assert _up(host, text=probe_conf(extra_if="MTU = 1380\n")).returncode == 0
    texts = [p.read_text(encoding="utf-8") for p in host.d.glob("setconf.*")]
    assert texts, "awg setconf не звался"
    for t in texts:
        assert "Address" not in t and "MTU" not in t, f"расширения awg-quick ушли в setconf:\n{t}"
        assert f"PrivateKey = {PRIV}" in t and "AllowedIPs = 169.254.88.1/32" in t


def test_up_stores_the_conf_0600_under_the_interface_name(host):
    import os
    import stat
    assert _up(host).returncode == 0
    dst = host.conf_dir / "awgqah.conf"
    assert dst.read_text(encoding="utf-8") == probe_conf(), "в каталог зондов лёг не тот конфиг"
    assert stat.S_IMODE(os.stat(dst).st_mode) == 0o600, "конфиг зонда с закрытым ключом не 0600"
    assert stat.S_IMODE(os.stat(host.conf_dir).st_mode) == 0o700
    assert not list(host.conf_dir.glob(".*.setconf")), "временный файл setconf с ключом остался"


def test_the_same_conf_again_is_up_same_and_touches_no_interface(host):
    assert _up(host).returncode == 0
    before = len([c for c in host.calls() if c.startswith("ip link add")])
    r = _up(host)
    assert r.returncode == 0 and "up: same" in r.stdout, r.stdout + r.stderr
    assert len([c for c in host.calls() if c.startswith("ip link add")]) == before, "повтор того же конфига пересоздал зонд"


def test_another_conf_on_a_standing_interface_goes_by_syncconf(host):
    """Сессия не рвётся: тот же интерфейс, новые ключи — syncconf, а не пересоздание."""
    assert _up(host).returncode == 0
    r = _up(host, text=probe_conf(addr="169.254.88.6/32"))
    assert r.returncode == 0 and "up: ok" in r.stdout
    tail = host.calls()[-20:]
    assert any(c.startswith("ip netns exec awg-quo awg syncconf awgqah") for c in tail), tail
    assert len([c for c in host.calls() if c.startswith("ip link add")]) == 1, "зонд пересоздан вместо syncconf"


def test_a_failed_syncconf_recreates_the_interface(host):
    assert _up(host).returncode == 0
    (host.d / "fail_syncconf").write_text("")
    r = _up(host, text=probe_conf(addr="169.254.88.6/32"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert len([c for c in host.calls() if c.startswith("ip link add")]) == 2, "после отказа syncconf зонд не пересоздан"
    assert host.links() == ["awgqah"]


def test_a_failed_interface_creation_leaves_no_conf_behind(host):
    (host.d / "fail_link_add").write_text("")
    r = _up(host)
    assert r.returncode != 0, "--up при неподнятом интерфейсе вернул успех"
    assert not (host.conf_dir / "awgqah.conf").exists(), "конфиг неподнятого зонда остался — --status врёт"


@pytest.mark.parametrize("name", ["awg0", "awgrsv", "eth0", "wg-quo"])
def test_up_refuses_an_interface_name_without_the_awgq_prefix(host, name):
    r = _up(host, name=name)
    assert r.returncode != 0 and "awgq" in r.stderr, r.stdout + r.stderr
    assert host.links() == [], "интерфейс с чужим именем создан"
    assert not list(host.conf_dir.glob("*.conf")), "конфиг под чужим именем лёг в каталог зондов"


def test_up_cuts_the_interface_name_to_safe_characters(host):
    r = _up(host, name="awgqah; rm -rf /")
    assert r.returncode == 0, r.stdout + r.stderr
    assert host.links() == ["awgqahrmrf"], host.links()


def test_up_refuses_a_bad_conf_before_touching_the_host(host):
    r = _up(host, text=probe_conf(peers=2))
    assert r.returncode != 0
    assert not any(c.startswith(("ip link", "ip netns add")) for c in host.calls()), "хост тронут до проверки конфига"


def test_without_root_up_refuses_and_touches_nothing(host):
    r = _up(host, FAKE_UID="1000")
    assert r.returncode != 0 and "root" in r.stderr
    assert host.calls() == [], "без root скрипт что-то делал"


# ── таблица awg_quo ──────────────────────────────────────────────────────────

def test_the_table_lets_in_only_replies_and_out_only_udp_to_the_responders(host):
    assert _up(host).returncode == 0
    assert _up(host, name="awgqar", text=probe_conf(resp="169.254.88.2/32")).returncode == 0
    table = host.tables()[-1]
    assert "table inet awg_quo {" in table
    assert "elements = { 169.254.88.1/32, 169.254.88.2/32 }" in table, "набор ответчиков — не из стоящих конфигов"
    inp = _chain(table, "input")
    assert inp[0] == "type filter hook input priority filter; policy drop;", inp
    assert "ct state established,related accept" in inp
    assert not any("dport" in ln or "saddr" in ln for ln in inp), f"во вход пространства пускается что-то кроме ответов: {inp}"
    out = _chain(table, "output")
    assert out[0] == "type filter hook output priority filter; policy drop;", out
    accepts = [ln for ln in out if ln.endswith("accept")]
    assert accepts == ['oifname "lo" accept', "ct state established,related accept",
                       "ip daddr @resp4 udp dport 8789 accept"], f"выход зонда шире ответчика: {out}"
    fwd = _chain(table, "forward")
    assert fwd == ["type filter hook forward priority filter; policy drop;"], f"пересылка в пространстве зондов: {fwd}"


def test_the_responder_port_follows_the_environment(host):
    assert _up(host, QUO_PORT="9911").returncode == 0
    assert "ip daddr @resp4 udp dport 9911 accept" in _chain(host.tables()[-1], "output")


def test_after_the_last_probe_goes_down_the_responder_set_is_empty(host):
    assert _up(host).returncode == 0
    r = host.sh("cmd_down awgqah")
    assert r.returncode == 0 and "down: ok" in r.stdout
    table = host.tables()[-1]
    assert "169.254.88.1" not in table, "ответчик снятого зонда остался в разрешённом выходе"
    assert "ip daddr @resp4 udp dport 8789 accept" in table
    assert host.links() == [] and not (host.conf_dir / "awgqah.conf").exists()


# ── --down, --wipe, --status ─────────────────────────────────────────────────

def test_wipe_takes_every_probe_its_conf_and_the_namespace(host):
    assert _up(host).returncode == 0
    assert _up(host, name="awgqar", text=probe_conf(resp="169.254.88.2/32")).returncode == 0
    r = host.sh("cmd_wipe")
    assert r.returncode == 0 and "wipe: ok" in r.stdout, r.stdout + r.stderr
    assert host.links() == [], "после --wipe зонды остались"
    assert not (host.d / "netns" / "awg-quo").exists(), "после --wipe пространство осталось"
    assert not host.conf_dir.exists(), "после --wipe каталог конфигов с ключами остался"


def test_wipe_twice_is_fine(host):
    assert host.sh("cmd_wipe").returncode == 0
    assert host.sh("cmd_wipe").returncode == 0


def test_status_without_the_namespace_says_absent(host):
    r = host.sh("cmd_status", FAKE_UID="1000")
    assert r.returncode == 0
    assert r.stdout.splitlines() == ["ns=absent", "ifaces="]


def test_status_prints_handshake_age_and_conf_presence_per_probe(host):
    assert _up(host).returncode == 0
    assert _up(host, name="awgqar", text=probe_conf(resp="169.254.88.2/32")).returncode == 0
    import time
    (host.d / "hs.awgqah").write_text(f"{PUB}\t{int(time.time()) - 30}\n")
    (host.d / "hs.awgqar").write_text(f"{PUB}\t0\n")
    lines = host.sh("cmd_status").stdout.splitlines()
    assert lines[0] == "ns=present" and lines[1] == "ifaces=awgqah awgqar", lines
    kv = dict(ln.split("=", 1) for ln in lines)
    assert kv["hs_awgqar"] == "never", "рукопожатия не было, а возраст выдуман"
    assert 29 <= int(kv["hs_awgqah"]) <= 40, kv
    assert kv["conf_awgqah"] == "present" and kv["conf_awgqar"] == "present"


def test_an_unknown_mode_prints_usage_and_exits_2(host):
    r = subprocess.run(["sh", str(SCRIPT), "--нет"], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{host.bin}:/usr/bin:/bin"})
    assert r.returncode == 2 and "--up" in r.stdout


def test_uninstall_takes_the_probe_namespace_away():
    """Снятие бота не оставляет пространства зондов и их конфигов на шлюзе."""
    src = (Path(__file__).resolve().parents[2] / "awg-bot.sh").read_text(encoding="utf-8")
    body = src.split("cmd_post_uninstall() {", 1)[1].split("\n}\n", 1)[0]
    assert "ip netns del awg-quo" in body and "rm -rf /var/lib/awg-gw/quorum" in body
