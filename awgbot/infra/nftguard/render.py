"""render.py — часть пакета nftguard (см. __init__)."""
from __future__ import annotations

from awgbot.infra import nftjson
from awgbot.infra import nftguard as pkg       # имена пакета — через него: их подменяют тесты


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


def render(spec: pkg.GuardSpec) -> str:
    """Полный текст файла таблицы. Без времени и прочего шума: текст равен
    тексту ⇔ состояние равно состоянию, этим и живёт дешёвая сверка."""
    p = spec.ssh_port
    head: list[str] = [
        "#!/usr/sbin/nft -f",
        "# awg-bot: единственная точка файервола хоста (таблица awg_bot_guard).",
        "# Файл генерирует бот — правки руками перезапишутся. Управление:",
        "#   awg-bot firewall status | setup | allow <ip> | deny <ip> | apply | off",
        "# Вайтлист и порты — conf/app.yaml (firewall.*), устройства админа — из БД.",
    ]
    nat_chain: list[str] = []
    if spec.nat and spec.tunnel_nets4:
        nat_chain = [
            "",
            "    chain postrouting {",
            "        type nat hook postrouting priority srcnat; policy accept;",
        ]
        rule = f"ip saddr @{pkg.SET_TUNNEL_NETS} masquerade"
        if spec.nat_exclude_ifs:
            rule = f"oifname != {nftjson.ifs(spec.nat_exclude_ifs)} " + rule
        nat_chain.append("        " + rule)
        nat_chain.append("    }")
    if not spec.filter:
        # NAT-only: файервол выключен, но клиентам нужен выход наружу.
        # Фильтрующих цепочек нет вовсе — политика хоста остаётся его. Одно
        # исключение: транзит линк ↔ линк — только между линками слотов с
        # VPN-транзитом (связь подсетей — его часть); при открытой политике
        # хоста закрыть остальные пары можно только здесь.
        block: list[str] = []
        if spec.peer_link_block:
            links = nftjson.ifs(spec.peer_link_block)
            rules = [f"        iifname {links} oifname {links} drop"]
            if spec.peer_link_ifs:
                transit = nftjson.ifs(spec.peer_link_ifs)
                rules = [f"        iifname {links} oifname {links} iifname != {transit} drop",
                         f"        iifname {links} oifname {links} oifname != {transit} drop"]
            block = ["", "    chain forward {",
                     "        type filter hook forward priority filter; policy accept;",
                     *rules,
                     "    }"]
        out = head + [
            "# Форма NAT-only: firewall.enabled=false, фильтра нет, только NAT клиентов.",
            f"table {pkg.TABLE}",
            f"delete table {pkg.TABLE}",
            f"table {pkg.TABLE} {{",
            pkg._set_block(pkg.SET_TUNNEL_NETS, "ipv4_addr", spec.tunnel_nets4, True),
        ] + nat_chain + block + ["}"]
        return "\n".join(out) + "\n"
    out: list[str] = head + [
        f"table {pkg.TABLE}",
        f"delete table {pkg.TABLE}",
        f"table {pkg.TABLE} {{",
        pkg._set_block(pkg.SET_ALLOW4, "ipv4_addr", spec.ssh_allow4, True),
        pkg._set_block(pkg.SET_ALLOW6, "ipv6_addr", spec.ssh_allow6, True),
        pkg._set_block(pkg.SET_TUNNEL_NETS, "ipv4_addr", spec.tunnel_nets4, True),
        pkg._set_block(pkg.SET_TUNNEL_ADMIN, "ipv4_addr", spec.tunnel_admin4, False),
        pkg._set_block(pkg.SET_LINK_PEERS, "ipv4_addr", spec.link_peers4, False),
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
        out.append(f"        udp dport {pkg._ports(spec.udp_ports)} accept")
    if spec.admin_dns4:
        # резолвер админа: имена SMB локальных сетей шлюзов — только устройствам
        # админа; правило раньше общего «:53 из туннеля», иначе то пустило бы всех
        out.append(f"        ip daddr {spec.admin_dns4} udp dport 53 ip saddr @{pkg.SET_TUNNEL_ADMIN} accept")
        out.append(f"        ip daddr {spec.admin_dns4} tcp dport 53 ip saddr @{pkg.SET_TUNNEL_ADMIN} accept")
        out.append(f"        ip daddr {spec.admin_dns4} drop")
    if spec.tunnel_nets4:
        out.append(f"        ip saddr @{pkg.SET_TUNNEL_NETS} udp dport 53 accept")
        out.append(f"        ip saddr @{pkg.SET_TUNNEL_NETS} tcp dport 53 accept")
    if spec.link_peers4 and spec.link_channel_port and spec.link_ifs:
        # Канал ВПС ↔ шлюз: вход только с адреса шлюза в /30 своего линка И
        # только из интерфейса линка: адрес /30 снаружи подделать можно, а
        # пакет, пришедший с публичного интерфейса, линком быть не может.
        # Клиентам туннеля сюда хода нет — их подсеть в другом наборе.
        ports = [int(spec.link_channel_port), *[int(p) for p in spec.link_channel_extra]]
        out.append(f"        iifname {nftjson.ifs(spec.link_ifs)} ip saddr @{pkg.SET_LINK_PEERS} "
                   f"tcp dport {pkg._ports(ports)} accept")
    if spec.open_tcp:
        out.append(f"        tcp dport {pkg._ports(spec.open_tcp)} accept")
    if spec.open_udp:
        out.append(f"        udp dport {pkg._ports(spec.open_udp)} accept")
    out.append("")
    if spec.tunnel_nets4:
        if spec.per_peer:
            out.append(f"        tcp dport {p} ip saddr @{pkg.SET_TUNNEL_ADMIN} accept")
            out.append(f"        tcp dport {p} ip saddr @{pkg.SET_TUNNEL_NETS} drop")
        else:
            out.append(f"        tcp dport {p} ip saddr @{pkg.SET_TUNNEL_NETS} accept")
    if spec.ssh_open:
        out.append(f"        tcp dport {p} accept")
    else:
        out.append(f"        tcp dport {p} ip saddr @{pkg.SET_ALLOW4} accept")
        out.append(f"        tcp dport {p} ip6 saddr @{pkg.SET_ALLOW6} accept")
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
        if spec.admin_dns4:
            # адрес резолвера админа — только локальный: пропади он с хоста,
            # запрос устройства админа не уходит маршрутом по умолчанию к провайдеру
            out.append(f"        ip daddr {spec.admin_dns4} drop")
        if spec.peer_link_ifs:
            # линк ↔ линк: связь подсетей между слотами с VPN-транзитом;
            # что именно едет, ограничивает AllowedIPs на стороне шлюза
            links = nftjson.ifs(spec.peer_link_ifs)
            out.append(f"        iifname {links} oifname {links} accept")
        if spec.tunnel_ifs:
            # пир → пир: только устройствам админа (шлюз, свои устройства);
            # остальным клиентам друг до друга пути нет
            ifs = nftjson.ifs(spec.tunnel_ifs)
            out.append(f"        iifname {ifs} oifname {ifs} ip saddr @{pkg.SET_TUNNEL_ADMIN} accept")
            out.append(f"        iifname {ifs} oifname {ifs} drop")
        if spec.tunnel_nets4:
            if spec.lan_space4 and spec.per_peer:
                # адреса в туннеле локальных сетей шлюзов — только устройствам
                # админа; второй рубеж после шлюза. Назначение — исходное
                # (ct original): к этому хуку адрес уже переведён в настоящий
                out.append(f"        ip saddr @{pkg.SET_TUNNEL_NETS} ip saddr != @{pkg.SET_TUNNEL_ADMIN} "
                           f"ct original ip daddr {spec.lan_space4} drop")
            out.append(f"        ip saddr @{pkg.SET_TUNNEL_NETS} accept")
            out.append(f"        ip daddr @{pkg.SET_TUNNEL_NETS} accept")
        out.append("    }")
    out += nat_chain
    out.append("}")
    return "\n".join(out) + "\n"

