"""collect.py — часть пакета nftguard (см. __init__)."""
from __future__ import annotations

import concurrent.futures as futures
import ipaddress
import re
import socket
import subprocess
import time
from pathlib import Path
from awgbot.core import config, settings
from awgbot.infra import nftguard as pkg       # имена пакета — через него: их подменяют тесты


# ── разбор вайтлиста: IP, CIDR, имя (DynDNS) ─────────────────────────────────
_resolve_cache: dict[str, tuple[float, list[str]]] = {}
_RESOLVE_TTL = 15 * 60
# У getaddrinfo своего таймаута нет: при молчащем резолвере вызов уходит в
# системный (обычно 5 с на каждый nameserver в resolv.conf), а зовут его и тик
# агента, и отрисовка раздела SSH — экран с кнопками просто замирал. Ждём
# ограниченно в отдельном потоке: не ответил вовремя — это ровно тот случай,
# на который рассчитан возврат прошлого значения, «DNS молчит».
_RESOLVE_TIMEOUT = 3.0


def _getaddrinfo_timed(host: str):
    """getaddrinfo с потолком ожидания — общая реализация (util/nets)."""
    from awgbot.util.nets import getaddrinfo_timed
    return getaddrinfo_timed(host, timeout=pkg._RESOLVE_TIMEOUT)


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
    hit = pkg._resolve_cache.get(host)
    if hit and hit[0] > now:
        return hit[1]
    try:
        infos = pkg._getaddrinfo_timed(host)
        ips = sorted({i[4][0].split("%")[0] for i in infos})
    except (socket.gaierror, OSError, futures.TimeoutError):
        # DNS не ответил: держим прошлое значение, а не роняем вайтлист —
        # иначе моргнувший резолвер запер бы админа снаружи
        return hit[1] if hit else []
    pkg._resolve_cache[host] = (now + pkg._RESOLVE_TTL, ips)
    return ips


def resolve_allow(entries) -> tuple[list[str], list[str], list[str]]:
    """Вайтлист из conf → (v4, v6, нерезолвящиеся имена). Порядок стабильный."""
    v4: list[str] = []
    v6: list[str] = []
    bad: list[str] = []
    for raw in entries or []:
        try:
            kind, val = pkg.classify(raw)
        except ValueError as e:
            pkg.log.warning("firewall.ssh_allow: %s — пропущено", e)
            continue
        if kind == "4":
            v4.append(val)
        elif kind == "6":
            v6.append(val)
        else:
            ips = pkg._resolve(val)
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
            pkg.log.warning("firewall: порт %r не число — пропущен", p)
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
        return pkg._docker_bridge_subnets()
    nets = [f"{config.SUBNET_PREFIX}.0/24"]
    if config.MIGRATION_INTERFACE and config.MIGRATION_SUBNET_PREFIX:
        m = f"{config.MIGRATION_SUBNET_PREFIX}.0/24"
        if m not in nets:
            nets.append(m)
    return nets


def build_spec(admin_ips, channel_extra=(), transit_links=()) -> pkg.GuardSpec:
    """Спецификация таблицы. admin_ips — адреса устройств админа, transit_links —
    линки слотов с VPN-транзитом (между ними открыт форвардинг линк ↔ линк):
    оба списка — из БД вызывающего; без БД (CLI до старта бота) — пусто, то
    есть транзит между линками закрыт."""
    from awgbot.infra.awg import in_container
    host_mode = not in_container()
    links = pkg.link_ifaces() if host_mode else []
    v4, v6, bad = pkg.resolve_allow(settings.get("app.firewall.ssh_allow", []) or [])
    ssh_open = not v4 and not v6
    admin = sorted({str(ipaddress.ip_address(a)) for a in (admin_ips or [])
                    if pkg._is_v4(a)}, key=ipaddress.ip_address)
    return pkg.GuardSpec(
        ssh_port=int(settings.get_int("app.network.ssh_port", config.SSH_PORT)),
        ssh_allow4=v4, ssh_allow6=v6, ssh_open=ssh_open,
        tunnel_nets4=pkg.tunnel_nets(), tunnel_admin4=admin if host_mode else [],
        tunnel_ifs=pkg._tunnel_ifs() if host_mode else [],
        per_peer=host_mode, udp_ports=pkg.listen_ports(),
        open_tcp=pkg._ports_from_conf(settings.get("app.firewall.open_tcp", [])),
        open_udp=pkg._ports_from_conf(settings.get("app.firewall.open_udp", [])),
        own_forward=host_mode, unresolved=bad,
        nat=host_mode, nat_exclude_ifs=pkg._nat_exclude_ifs() if host_mode else [],
        filter=pkg.enabled(),
        peer_link_ifs=pkg._peer_link_ifs(links, transit_links),
        peer_link_block=links if len(links) >= 2 else [],
        link_peers4=pkg.link_peer_addrs() if host_mode else [],
        link_ifs=links,
        link_channel_port=pkg.link_channel_port() if host_mode else 0,
        # прежний порт канала после смены — неделю открыт тоже (gwcfg_channel_ports)
        link_channel_extra=[int(p) for p in channel_extra if int(p) and int(p) != pkg.link_channel_port()] if host_mode else [],
        admin_dns4=_admin_dns_addr() if host_mode else "",
        lan_space4=_lan_space() if host_mode else "",
        wan_ifs=pkg.wan_ifaces() if host_mode else [],
        quarantine_ifs=pkg.quarantine_ifaces(links) if host_mode else [],
        quarantine_pairs=quarantine_pairs(pkg.quarantine_ifaces(links)) if host_mode else [],
    )


JOIN_STAMP = "# awg-bot: сопряжение "


def quarantine_pairs(names: list[str]) -> list[tuple[str, str, str]]:
    """(линк, адрес ВПС, адрес шлюза) линков сопряжения — по Address стороны
    ВПС в /30 (шлюз — следующий адрес): канал открыт только этой паре."""
    out = []
    for name in names:
        try:
            text = Path(f"{config.AWG_DIR}/{name}.conf").read_text(encoding="utf-8")
            addr = next(line.split("=", 1)[1].strip() for line in text.splitlines()
                        if line.strip().lower().startswith("address"))
            iface = ipaddress.ip_interface(addr.split(",")[0].strip())
        except (OSError, StopIteration, ValueError):
            continue
        if iface.version != 4:
            continue
        out.append((name, str(iface.ip), str(iface.ip + 1)))
    return out


def quarantine_ifaces(links: list[str]) -> list[str]:
    """Линки сопряжения — по штампу в их конфиге (routing-link-setup.sh
    --join-stage), без БД: смена ключей после кода пишет обычный конфиг, и
    карантин снят по построению."""
    out: list[str] = []
    for name in links:
        try:
            text = Path(f"{config.AWG_DIR}/{name}.conf").read_text(encoding="utf-8")
        except OSError:
            continue
        if any(line.startswith(JOIN_STAMP) for line in text.splitlines()[:5]):
            out.append(name)
    return out


def _lan_space() -> str:
    """Служебное пространство адресов в туннеле локальных сетей шлюзов: в него
    из туннеля — только устройствам админа. Из conf, без БД."""
    from awgbot.util import lanspace
    return str(lanspace.space())


def _admin_dns_addr() -> str:
    """Адрес резолвера админа, если он поставлен: его :53 — только устройствам
    админа (имена SMB локальных сетей шлюзов прочие клиенты видеть не должны)."""
    from awgbot.infra import admindns
    return admindns.addr() if admindns.installed() else ""


def _peer_link_ifs(links: list[str], transit) -> list[str]:
    """Линки, между которыми открыт форвардинг линк ↔ линк: линки слотов с
    VPN-транзитом (связь подсетей — его часть), которые есть на хосте, — только
    при двух и больше. Без наборов подсетей: что едет, ограничивает AllowedIPs
    на стороне шлюза."""
    want = [str(t) for t in (transit or ()) if str(t) in links]
    out = list(dict.fromkeys(want))
    return out if len(out) >= 2 else []


_IFNAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,15}")


def wan_ifaces() -> list[str]:
    """Внешние интерфейсы хоста — устройства маршрутов по умолчанию основной
    таблицы, кроме awg-интерфейсов и линков. В них частным адресам назначения
    из туннеля не место (render: «частное — не в WAN»). Не прочиталось — пусто,
    правила нет: таблица собирается и из CLI, и до подъёма сети."""
    import json
    try:
        out = subprocess.run(["ip", "-j", "route", "show", "default"], capture_output=True, timeout=5).stdout
        routes = json.loads(out.decode(errors="replace") or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    skip = set(pkg._nat_exclude_ifs())
    devs: list[str] = []
    for r in routes if isinstance(routes, list) else []:
        dev = str((r or {}).get("dev") or "")
        if dev and dev not in skip and dev not in devs and _IFNAME_RE.fullmatch(dev):
            devs.append(dev)
    return devs


def _tunnel_ifs() -> list[str]:
    from awgbot.infra.awg import gated_ifaces
    return [i for i in gated_ifaces() if i]


def link_ifaces() -> list[str]:
    """Линки до шлюзов: интерфейс из конфига плюс
    все конфиги в каталоге awg с `Table = off` — так выглядит только линк,
    у клиентских интерфейсов таблицу ведёт awg-quick. Без БД: этот модуль
    зовётся и из CLI `awg-bot firewall`, где бота нет."""
    out: list[str] = []
    if config.ROUTING_GW_INTERFACE:
        out.append(config.ROUTING_GW_INTERFACE)
    try:
        for p in sorted(Path(config.AWG_DIR).glob("*.conf")):
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue
            if re.search(r"(?m)^\s*Table\s*=\s*off\b", text) and p.stem not in out:
                out.append(p.stem)
    except OSError:
        pass
    return out


def link_peer_addrs() -> list[str]:
    """Адреса шлюзов в /30 линков — из конфигов линков, без БД и без exec.

    Адрес ВПС в конфиге есть всегда (`Address = …/30`), а второй хост /30
    вычисляется однозначно: адресов там ровно два. Конфиг, а не ядро, потому
    что таблицу собирает и CLI, и делает это до подъёма интерфейсов —
    fail-closed с загрузки важнее свежести.
    """
    out: list[str] = []
    for name in pkg.link_ifaces():
        try:
            text = Path(f"{config.AWG_DIR}/{name}.conf").read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r"(?m)^\s*Address\s*=\s*([0-9.]+/\d+)", text)
        if not m:
            continue
        try:
            iface = ipaddress.ip_interface(m.group(1))
        except ValueError:
            continue
        if iface.network.prefixlen != 30:
            continue
        for host in iface.network.hosts():
            if host != iface.ip and str(host) not in out:
                out.append(str(host))
    return out


def link_channel_port() -> int:
    """Порт канала; 0 — канала нет вовсе (ни одного линка)."""
    if not pkg.link_ifaces():
        return 0
    from awgbot.util import gwlink
    return gwlink.channel_port()


def _nat_exclude_ifs() -> list[str]:
    """Интерфейсы, в которые трафик клиентов уходит БЕЗ маскарада: клиентские
    awg (пир → пир), линки до шлюзов (шлюз маскарадит сам и должен видеть
    настоящий адрес клиента для исключений). Наружу (WAN) — всё остальное."""
    out = list(pkg._tunnel_ifs())
    for name in pkg.link_ifaces():
        if name not in out:
            out.append(name)
    return out


def _is_v4(a) -> bool:
    try:
        return ipaddress.ip_address(str(a)).version == 4
    except ValueError:
        return False

