"""
nftguard.py — ЕДИНСТВЕННАЯ точка файервола хоста бота: nft-таблица
`inet awg_bot_guard`, которую целиком ведёт бот.

До этого на пути SSH-пакета из туннеля стояло четверо ворот (nft-таблица
harden'а, цепочка iptables AWGBOT_SSH, PostUp-страж в conf интерфейса, sshd
Match Address) плюс ufw рядом, и отказ на любых выглядел одинаково. Теперь
ворота одни:

  • статика (порт SSH, внешний вайтлист, порты интерфейсов, подсети туннеля,
    политика INPUT/FORWARD) — из conf/app.yaml (`firewall.*`, `network.*`);
  • динамика (адреса устройств админа, которым открыт SSH из туннеля) — set
    `ssh_tunnel4`, бот правит его по событию и сверяет каждый тик;
  • всё вместе лежит в /etc/nftables.d/awg-bot-guard.nft, который грузит
    nftables.service ДО подъёма интерфейсов — fail-closed с загрузки, без
    PostUp-строк и без окна «интерфейс поднялся, бот ещё не реассертил».

Применение — всегда атомарная замена таблицы (`table X; delete table X;
table X {…}` одним `nft -f`): нет момента с полупустой цепочкой. Сверка по
тику дешёвая: текст желаемой таблицы сравнивается с файлом (без exec), живой
set — одним `nft -j list set`.

Режимы. В host-режиме хост видит пиров с настоящими адресами → пер-пирный SSH
(set устройств админа) и своя цепочка FORWARD. В docker-режиме пиры приходят
с bridge-адреса контейнера и неразличимы → SSH из туннеля решается по
bridge-подсети целиком, FORWARD оставлен docker'у.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from awgbot.core import config, settings

log = logging.getLogger("awgbot.nftguard")

TABLE_FAMILY = "inet"
TABLE_NAME = "awg_bot_guard"
TABLE = f"{TABLE_FAMILY} {TABLE_NAME}"
RULES_DIR = "/etc/nftables.d"
RULES_FILE = f"{RULES_DIR}/awg-bot-guard.nft"
MAIN_CONF = "/etc/nftables.conf"
ROLLBACK_UNIT = "awg-bot-fw-rollback"
ROLLBACK_SECONDS = 180

SET_ALLOW4 = "ssh_allow4"
SET_ALLOW6 = "ssh_allow6"
SET_TUNNEL_NETS = "tunnel_nets4"
SET_TUNNEL_ADMIN = "ssh_tunnel4"


class GuardError(RuntimeError):
    """nft/файл/резолв не удались — вызывающий решает, логировать или падать."""


# ── спецификация таблицы ─────────────────────────────────────────────────────

@dataclass
class GuardSpec:
    ssh_port: int
    ssh_allow4: list[str] = field(default_factory=list)   # IP/CIDR v4 (вайтлист)
    ssh_allow6: list[str] = field(default_factory=list)
    ssh_open: bool = False            # вайтлист пуст → SSH открыт всем (осознанно)
    tunnel_nets4: list[str] = field(default_factory=list)  # подсети туннеля (host) / bridge (docker)
    tunnel_admin4: list[str] = field(default_factory=list) # устройства админа (host)
    per_peer: bool = True             # host: SSH из туннеля только устройствам админа
    udp_ports: list[int] = field(default_factory=list)     # порты интерфейсов awg
    open_tcp: list[int] = field(default_factory=list)      # прочие порты хоста
    open_udp: list[int] = field(default_factory=list)
    own_forward: bool = True          # host: своя цепочка FORWARD (policy drop)
    unresolved: list[str] = field(default_factory=list)    # имена, которые не резолвятся


def enabled() -> bool:
    return settings.get_bool("app.firewall.enabled", False)


# ── разбор вайтлиста: IP, CIDR, имя (DynDNS) ─────────────────────────────────
_resolve_cache: dict[str, tuple[float, list[str]]] = {}
_RESOLVE_TTL = 15 * 60


def classify(entry: str) -> tuple[str, str]:
    """('4'|'6'|'host', нормализованное значение). Некорректное → ValueError."""
    e = str(entry).strip()
    if not e:
        raise ValueError("пустая запись")
    try:
        net = ipaddress.ip_network(e, strict=False)
        return ("4" if net.version == 4 else "6"), str(net)
    except ValueError:
        pass
    if re.fullmatch(r"[A-Za-z0-9.-]{1,253}", e) and "." in e and not e[0].isdigit():
        return "host", e.lower()
    raise ValueError(f"не IP, не CIDR и не имя хоста: {e!r}")


def _resolve(host: str) -> list[str]:
    now = time.monotonic()
    hit = _resolve_cache.get(host)
    if hit and hit[0] > now:
        return hit[1]
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0].split("%")[0] for i in infos})
    except (socket.gaierror, OSError):
        # DNS не ответил: держим прошлое значение, а не роняем вайтлист —
        # иначе моргнувший резолвер запер бы админа снаружи
        return hit[1] if hit else []
    _resolve_cache[host] = (now + _RESOLVE_TTL, ips)
    return ips


def resolve_allow(entries) -> tuple[list[str], list[str], list[str]]:
    """Вайтлист из conf → (v4, v6, нерезолвящиеся имена). Порядок стабильный."""
    v4: list[str] = []
    v6: list[str] = []
    bad: list[str] = []
    for raw in entries or []:
        try:
            kind, val = classify(raw)
        except ValueError as e:
            log.warning("firewall.ssh_allow: %s — пропущено", e)
            continue
        if kind == "4":
            v4.append(val)
        elif kind == "6":
            v6.append(val)
        else:
            ips = _resolve(val)
            if not ips:
                bad.append(val)
            for ip in ips:
                try:
                    net = str(ipaddress.ip_network(ip))    # 203.0.113.9 → …/32, как CIDR
                except ValueError:
                    continue
                (v4 if ":" not in ip else v6).append(net)
    return sorted(set(v4), key=ipaddress.ip_network), sorted(set(v6), key=ipaddress.ip_network), bad


# ── сбор спецификации из conf и живого хоста ─────────────────────────────────

def _ports_from_conf(entries) -> list[int]:
    out: list[int] = []
    for p in entries or []:
        try:
            n = int(p)
        except (TypeError, ValueError):
            log.warning("firewall: порт %r не число — пропущен", p)
            continue
        if 1 <= n <= 65535 and n not in out:
            out.append(n)
    return out


def listen_ports() -> list[int]:
    """ListenPort всех *.conf в каталоге awg: интерфейсы клиентов (в т.ч.
    переезда) и линк до шлюза. Плюс network.server_port из conf — на случай,
    если каталог недоступен (docker-режим: conf внутри контейнера)."""
    ports: list[int] = []
    try:
        for p in sorted(Path(config.AWG_DIR).glob("*.conf")):
            try:
                m = re.search(r"(?m)^\s*ListenPort\s*=\s*(\d+)", p.read_text(encoding="utf-8"))
            except OSError:
                continue
            if m:
                n = int(m.group(1))
                if n not in ports:
                    ports.append(n)
    except OSError:
        pass
    if config.SERVER_PORT and config.SERVER_PORT not in ports:
        ports.append(int(config.SERVER_PORT))
    return ports


def _docker_bridge_subnets() -> list[str]:
    try:
        out = subprocess.run(
            ["docker", "inspect", config.CONTAINER, "-f",
             "{{range $k,$v := .NetworkSettings.Networks}}{{$k}}\n{{end}}"],
            capture_output=True, timeout=10).stdout.decode(errors="replace")
        nets = [n for n in out.split() if n]
        subnets: list[str] = []
        for n in nets:
            o = subprocess.run(["docker", "network", "inspect", n, "-f",
                                "{{range .IPAM.Config}}{{.Subnet}}\n{{end}}"],
                               capture_output=True, timeout=10).stdout.decode(errors="replace")
            for s in o.split():
                if ":" not in s and s not in subnets:
                    subnets.append(s)
        return subnets
    except (OSError, subprocess.SubprocessError):
        return []


def tunnel_nets() -> list[str]:
    from awgbot.infra.awg import in_container
    if in_container():
        return _docker_bridge_subnets()
    nets = [f"{config.SUBNET_PREFIX}.0/24"]
    if config.MIGRATION_INTERFACE and config.MIGRATION_SUBNET_PREFIX:
        m = f"{config.MIGRATION_SUBNET_PREFIX}.0/24"
        if m not in nets:
            nets.append(m)
    return nets


def build_spec(admin_ips) -> GuardSpec:
    from awgbot.infra.awg import in_container
    host_mode = not in_container()
    v4, v6, bad = resolve_allow(settings.get("app.firewall.ssh_allow", []) or [])
    ssh_open = not v4 and not v6
    admin = sorted({str(ipaddress.ip_address(a)) for a in (admin_ips or [])
                    if _is_v4(a)}, key=ipaddress.ip_address)
    return GuardSpec(
        ssh_port=int(settings.get_int("app.network.ssh_port", config.SSH_PORT)),
        ssh_allow4=v4, ssh_allow6=v6, ssh_open=ssh_open,
        tunnel_nets4=tunnel_nets(), tunnel_admin4=admin if host_mode else [],
        per_peer=host_mode, udp_ports=listen_ports(),
        open_tcp=_ports_from_conf(settings.get("app.firewall.open_tcp", [])),
        open_udp=_ports_from_conf(settings.get("app.firewall.open_udp", [])),
        own_forward=host_mode, unresolved=bad,
    )


def _is_v4(a) -> bool:
    try:
        return ipaddress.ip_address(str(a)).version == 4
    except ValueError:
        return False


# ── рендер таблицы ───────────────────────────────────────────────────────────

def _set_block(name: str, typ: str, elems: list[str], interval: bool) -> str:
    lines = [f"    set {name} {{", f"        type {typ}"]
    if interval:
        lines.append("        flags interval")
    if elems:
        lines.append("        elements = { " + ", ".join(elems) + " }")
    lines.append("    }")
    return "\n".join(lines)


def _ports(ps: list[int]) -> str:
    return "{ " + ", ".join(str(p) for p in ps) + " }" if len(ps) > 1 else str(ps[0])


def render(spec: GuardSpec) -> str:
    """Полный текст файла таблицы. Без времени и прочего шума: текст равен
    тексту ⇔ состояние равно состоянию, этим и живёт дешёвая сверка."""
    p = spec.ssh_port
    out: list[str] = [
        "#!/usr/sbin/nft -f",
        "# awg-bot: единственная точка файервола хоста (таблица awg_bot_guard).",
        "# Файл генерирует бот — правки руками перезапишутся. Управление:",
        "#   awg-bot firewall status | setup | allow <ip> | deny <ip> | apply | off",
        "# Вайтлист и порты — conf/app.yaml (firewall.*), устройства админа — из БД.",
        f"table {TABLE}",
        f"delete table {TABLE}",
        f"table {TABLE} {{",
        _set_block(SET_ALLOW4, "ipv4_addr", spec.ssh_allow4, True),
        _set_block(SET_ALLOW6, "ipv6_addr", spec.ssh_allow6, True),
        _set_block(SET_TUNNEL_NETS, "ipv4_addr", spec.tunnel_nets4, True),
        _set_block(SET_TUNNEL_ADMIN, "ipv4_addr", spec.tunnel_admin4, False),
        "",
        "    chain input {",
        "        type filter hook input priority filter; policy drop;",
        "        ct state established,related accept",
        "        ct state invalid drop",
        '        iifname "lo" accept',
        "        ip protocol icmp accept",
        "        meta l4proto ipv6-icmp accept",
    ]
    if spec.udp_ports:
        out.append(f"        udp dport {_ports(spec.udp_ports)} accept")
    if spec.tunnel_nets4:
        out.append(f"        ip saddr @{SET_TUNNEL_NETS} udp dport 53 accept")
        out.append(f"        ip saddr @{SET_TUNNEL_NETS} tcp dport 53 accept")
    if spec.open_tcp:
        out.append(f"        tcp dport {_ports(spec.open_tcp)} accept")
    if spec.open_udp:
        out.append(f"        udp dport {_ports(spec.open_udp)} accept")
    out.append("")
    if spec.tunnel_nets4:
        if spec.per_peer:
            out.append(f"        tcp dport {p} ip saddr @{SET_TUNNEL_ADMIN} accept")
            out.append(f"        tcp dport {p} ip saddr @{SET_TUNNEL_NETS} drop")
        else:
            out.append(f"        tcp dport {p} ip saddr @{SET_TUNNEL_NETS} accept")
    if spec.ssh_open:
        out.append(f"        tcp dport {p} accept")
    else:
        out.append(f"        tcp dport {p} ip saddr @{SET_ALLOW4} accept")
        out.append(f"        tcp dport {p} ip6 saddr @{SET_ALLOW6} accept")
        out.append(f"        tcp dport {p} drop")
    out.append("    }")
    if spec.own_forward:
        out += [
            "",
            "    chain forward {",
            "        type filter hook forward priority filter; policy drop;",
            "        ct state established,related accept",
            "        ct state invalid drop",
        ]
        if spec.tunnel_nets4:
            out.append(f"        ip saddr @{SET_TUNNEL_NETS} accept")
            out.append(f"        ip daddr @{SET_TUNNEL_NETS} accept")
        out.append("    }")
    out.append("}")
    return "\n".join(out) + "\n"


# ── nft: применить, прочитать ────────────────────────────────────────────────

def _nft(args: list[str], timeout: int = 10, check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["nft", *args], capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise GuardError("nft не найден — установите пакет nftables")
    except subprocess.TimeoutExpired:
        raise GuardError(f"таймаут nft {' '.join(args[:3])}")
    if check and proc.returncode != 0:
        raise GuardError(f"nft {' '.join(args[:3])}: "
                         f"{proc.stderr.decode(errors='replace').strip()}")
    return proc


def read_file() -> str:
    try:
        return Path(RULES_FILE).read_text(encoding="utf-8")
    except OSError:
        return ""


def write_file(text: str) -> None:
    d = Path(RULES_DIR)
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".awg-bot-guard.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, RULES_FILE)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise GuardError(f"не записать {RULES_FILE}: {e}")


def check_text(text: str) -> None:
    """Синтаксис — до записи: `nft -c -f`."""
    with tempfile.NamedTemporaryFile("w", suffix=".nft", delete=False, encoding="utf-8") as f:
        f.write(text)
        tmp = f.name
    try:
        _nft(["-c", "-f", tmp])
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def apply_text(text: str) -> None:
    """Проверить, записать, применить атомарно. Порядок важен: файл — источник
    для загрузки при ребуте, и он не должен опережать проверку синтаксиса."""
    check_text(text)
    write_file(text)
    _nft(["-f", RULES_FILE])


def table_present() -> bool:
    return _nft(["list", "table", TABLE_FAMILY, TABLE_NAME], check=False).returncode == 0


def live_set(name: str) -> Optional[set[str]]:
    """Элементы set из ядра; None — таблицы/set нет."""
    proc = _nft(["-j", "list", "set", TABLE_FAMILY, TABLE_NAME, name], check=False)
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout.decode(errors="replace") or "{}")
    except json.JSONDecodeError as e:
        raise GuardError(f"nft -j: {e}")
    out: set[str] = set()
    for item in doc.get("nftables", []):
        s = item.get("set")
        if not s:
            continue
        for el in s.get("elem", []) or []:
            out.add(_elem_str(el))
    return out


def _elem_str(el) -> str:
    if isinstance(el, str):
        return el
    if isinstance(el, dict):
        if "prefix" in el:
            return f"{el['prefix']['addr']}/{el['prefix']['len']}"
        if "elem" in el:                       # {"elem": {"val": ..., ...}}
            return _elem_str(el["elem"].get("val"))
        if "val" in el:
            return _elem_str(el["val"])
    return str(el)


# ── сверка по тику ───────────────────────────────────────────────────────────

def reconcile(admin_ips) -> str:
    """Привести таблицу к желаемой. Возвращает, что сделано:
    'disabled' | 'ok' | 'applied' (текст изменился) | 'restored' (таблицу
    снесли) | 'resynced' (set устройств разошёлся с файлом)."""
    if not enabled():
        return "disabled"
    spec = build_spec(admin_ips)
    text = render(spec)
    if text != read_file():
        apply_text(text)
        return "applied"
    live = live_set(SET_TUNNEL_ADMIN)
    if live is None:
        apply_text(text)
        return "restored"
    if live != set(spec.tunnel_admin4):
        apply_text(text)
        return "resynced"
    return "ok"


# ── персистентность, откат, снятие ───────────────────────────────────────────

def ensure_persistence() -> list[str]:
    """include нашего каталога в /etc/nftables.conf + nftables.service enabled.
    Возвращает список сделанного (для вывода в CLI)."""
    done: list[str] = []
    include = f'include "{RULES_DIR}/*.nft"'
    main = Path(MAIN_CONF)
    try:
        cur = main.read_text(encoding="utf-8") if main.exists() else ""
        if RULES_DIR not in cur:
            with main.open("a", encoding="utf-8") as f:
                if not cur:
                    f.write("#!/usr/sbin/nft -f\n")
                f.write(f"\n{include}\n")
            done.append(f"include в {MAIN_CONF}")
    except OSError as e:
        raise GuardError(f"{MAIN_CONF}: {e}")
    rc = subprocess.run(["systemctl", "enable", "nftables"], capture_output=True).returncode
    if rc == 0:
        done.append("nftables.service включён")
    else:
        done.append("ВНИМАНИЕ: systemctl enable nftables не удался — правила могут не пережить ребут")
    return done


def rollback_armed() -> bool:
    rc = subprocess.run(["systemctl", "is-active", "--quiet", f"{ROLLBACK_UNIT}.timer"],
                        capture_output=True).returncode
    return rc == 0


def arm_rollback(seconds: int, command: list[str], env: dict) -> None:
    """Таймер отката: через N секунд без `confirm` таблица снимается и
    firewall.enabled сбрасывается. Страховка от самозапирания."""
    disarm_rollback()
    args = ["systemd-run", f"--unit={ROLLBACK_UNIT}", f"--on-active={int(seconds)}s",
            "--collect", "--quiet"]
    for k, v in env.items():
        args.append(f"--setenv={k}={v}")
    proc = subprocess.run(args + command, capture_output=True)
    if proc.returncode != 0:
        raise GuardError("таймер отката не поставлен: "
                         + proc.stderr.decode(errors="replace").strip())


def disarm_rollback() -> bool:
    was = rollback_armed()
    for unit in (f"{ROLLBACK_UNIT}.timer", f"{ROLLBACK_UNIT}.service"):
        subprocess.run(["systemctl", "stop", unit], capture_output=True)
        subprocess.run(["systemctl", "reset-failed", unit], capture_output=True)
    return was


def remove() -> list[str]:
    """Снять таблицу и файл; SSH становится открыт всем (доступ не теряется)."""
    done: list[str] = []
    if _nft(["delete", "table", TABLE_FAMILY, TABLE_NAME], check=False).returncode == 0:
        done.append("таблица снята")
    p = Path(RULES_FILE)
    if p.exists():
        try:
            p.replace(RULES_FILE + ".off")
            done.append(f"файл перенесён в {RULES_FILE}.off")
        except OSError as e:
            done.append(f"файл не перенесён: {e}")
    return done


def ufw_active() -> bool:
    try:
        out = subprocess.run(["ufw", "status"], capture_output=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return b"Status: active" in out


def status(admin_ips) -> dict:
    spec = build_spec(admin_ips)
    text = render(spec)
    present = table_present()
    return {
        "enabled": enabled(), "present": present, "file": read_file() == text,
        "live_admin": live_set(SET_TUNNEL_ADMIN) if present else None,
        "live_allow4": live_set(SET_ALLOW4) if present else None,
        "spec": spec, "rollback": rollback_armed(), "ufw": ufw_active(),
    }
