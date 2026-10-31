"""allow.py — часть пакета nftguard (см. __init__): вайтлист SSH для таблицы —
адреса имён с удержанием прежних, наборы без пересечений."""
from __future__ import annotations

import ipaddress
import re

from awgbot.infra import nftguard as pkg       # имена пакета — через него: их подменяют тесты

# Адреса имён вайтлиста — строками-комментариями в файле таблицы: файл и есть
# последний применённый набор, он переживает рестарт бота и ребут хоста.
HOST_MARK = "# ssh_allow: "
_HOST_LINE = re.compile(r"^# ssh_allow: ([A-Za-z0-9.-]{1,253}) = (.+)$")


def noverlap(elems) -> list[str]:
    """Элементы набора `flags interval` без пересечений: nft отвергает всю
    таблицу на «conflicting intervals» (подсеть и адрес в ней, имя DynDNS в
    подсеть списка, анонс Telegram шире выученной сети) — таблица застывала
    прежней, а сверка падала каждый тик. Пересечения схлопываются; без них —
    как есть, порядок не трогаем (текст таблицы = её состояние)."""
    elems = list(elems)
    try:
        nets = [ipaddress.ip_network(str(e), strict=False) for e in elems]
    except ValueError:
        return elems                                   # не адреса — не наш случай
    overlap = False
    for fam in (4, 6):
        end = None
        for n in sorted((n for n in nets if n.version == fam), key=lambda n: (n.network_address, -n.prefixlen)):
            if end is not None and n.network_address <= end:
                overlap = True
                break
            end = n.broadcast_address
        if overlap:
            break
    if not overlap:
        return elems
    out: list[str] = []
    for fam in (4, 6):
        out += [str(n) for n in ipaddress.collapse_addresses(n for n in nets if n.version == fam)]
    return out


def file_hosts() -> dict[str, list[str]]:
    """Имя → адреса из прежнего файла таблицы (строки HOST_MARK); нет — пусто."""
    out: dict[str, list[str]] = {}
    for line in pkg.read_file().splitlines():
        m = _HOST_LINE.match(line.strip())
        if not m:
            continue
        nets = []
        for tok in m.group(2).split(","):
            try:
                nets.append(str(ipaddress.ip_network(tok.strip(), strict=False)))
            except ValueError:
                continue
        if nets:
            out[m.group(1).lower()] = nets
    return out


def resolve_entries(entries) -> tuple[list[str], list[str], list[str], dict[str, list[str]]]:
    """Вайтлист из conf → (v4, v6, нерезолвящиеся имена, имя → его адреса)."""
    v4: list[str] = []
    v6: list[str] = []
    bad: list[str] = []
    hosts: dict[str, list[str]] = {}
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
            nets = []
            for ip in ips:
                try:
                    nets.append(str(ipaddress.ip_network(ip)))     # 203.0.113.9 → …/32, как CIDR
                except ValueError:
                    continue
            if nets:
                hosts[val] = sorted(set(nets), key=ipaddress.ip_network)
            for net in nets:
                (v4 if ":" not in net else v6).append(net)
    return v4, v6, bad, hosts


def ssh_allow(entries) -> dict:
    """Наборы вайтлиста для таблицы: {'v4', 'v6', 'bad', 'hosts', 'held'}.

    Имя не резолвится (DNS молчит, а кэша в памяти нет — после рестарта
    бота) — держим его адреса из прежнего файла таблицы (`held`): иначе
    список из одних имён давал пустые наборы, и фильтр открывал SSH всем.
    Наборы — без пересечений (noverlap)."""
    v4, v6, bad, hosts = pkg.resolve_entries(entries)
    held: list[str] = []
    if bad:
        prev = pkg.file_hosts()
        for name in bad:
            nets = prev.get(name)
            if not nets:
                continue
            held.append(name)
            hosts[name] = nets
            for net in nets:
                (v4 if ":" not in net else v6).append(net)
    if held:
        pkg.log.warning("firewall.ssh_allow: не резолвятся %s — держу прежние адреса из таблицы", ", ".join(held))
    return {"v4": pkg.noverlap(sorted(set(v4), key=ipaddress.ip_network)),
            "v6": pkg.noverlap(sorted(set(v6), key=ipaddress.ip_network)),
            "bad": bad, "hosts": hosts, "held": held}


def host_lines(hosts: dict[str, list[str]]) -> list[str]:
    """Строки-комментарии файла таблицы: адреса имён вайтлиста (file_hosts)."""
    return [f"{HOST_MARK}{name} = {', '.join(nets)}" for name, nets in sorted(hosts.items()) if nets]
