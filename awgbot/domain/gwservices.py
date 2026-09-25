"""
gwservices.py — сервисы соседних сетей (концепт «сервисы соседних сетей»).

SMB-серверы локальной сети одного шлюза видны в Finder на Mac в сети другого
шлюза без multicast через туннели: агент-источник находит их по mDNS
(`avahi-browse`), список едет каналом линка на сервер AWG, тот раздаёт его
шлюзам-соседям по функции «доступ между подсетями», а агент-получатель кладёт
записи DNS-SD (RFC 6763) в dnsmasq под доменом обзора. Модуль общий для обеих
ролей: формат записи и чистка обязаны совпадать на концах, а сборка файла
dnsmasq — единственная точка, где данные с чужой машины превращаются в
конфиг. Ни одного сетевого вызова и чтения с хоста здесь нет — чистые функции.

Запись: {"t": тип, "n": имя инстанса (что покажет Finder), "h": метка хоста в
домене обзора, "p": порт, "a": IPv4}. Только ASCII в именах: dnsmasq в Debian
собран с IDN2 и переводит не-ASCII в punycode, а Finder показал бы «xn--…».
Регистр dnsmasq приводит к нижнему — известное ограничение, в чистке его не
сохраняем нарочно.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re

SERVICE_TYPES = ("_smb._tcp",)          # пока только SMB: шаблоны файла и помощника завязаны на него
BROWSE_DOMAIN = "awg.internal"          # зарезервирован ICANN, апстримом не уходит
MAX_OWN = 32                            # записей у одного слота
MAX_PEER = 64                           # записей у получателя в сумме
CONF_NAME = "awg-gw-peer-services.conf" # файл в /etc/dnsmasq.d на получателе

_NAME_RE = re.compile(r"[A-Za-z0-9 _-]{1,63}")
_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_NAME_STRIP_RE = re.compile(r"[^A-Za-z0-9 _-]+")
_HOST_STRIP_RE = re.compile(r"[^A-Za-z0-9-]+")
_D = BROWSE_DOMAIN.replace(".", r"\.")
# Построчный белый список файла dnsmasq — те же шаблоны, что в помощнике
# awg-lan-services.sh (routing-gw-setup.sh): последний рубеж на малине против
# server=/address=/conf-file= из канала. Агент проверяет им файл до записи
# (lines_ok), помощник — своим grep; тест сверяет оба набора.
LINE_RES = tuple(re.compile(p) for p in (
    r"^#.*$",
    r"^local=/" + _D + r"/$",
    r"^ptr-record=l?b\._dns-sd\._udp\.([0-9]{1,3}\.){4}in-addr\.arpa," + _D + r"$",
    r"^ptr-record=l?b\._dns-sd\._udp\." + _D + r"," + _D + r"$",
    r"^ptr-record=_services\._dns-sd\._udp\." + _D + r",_smb\._tcp\." + _D + r"$",
    r'^ptr-record=_smb\._tcp\.' + _D + r',"[A-Za-z0-9 _-]{1,63}\._smb\._tcp\.' + _D + r'"$',
    r'^srv-host="[A-Za-z0-9 _-]{1,63}\._smb\._tcp\.' + _D + r'",[A-Za-z0-9-]{1,63}\.' + _D + r',[0-9]{1,5}$',
    r'^txt-record="[A-Za-z0-9 _-]{1,63}\._smb\._tcp\.' + _D + r'",""$',
    r"^host-record=[A-Za-z0-9-]{1,63}\." + _D + r",[0-9]{1,3}(\.[0-9]{1,3}){3}$",
))


def _nets(nets) -> list[ipaddress.IPv4Network]:
    out = []
    for n in nets or []:
        try:
            out.append(ipaddress.IPv4Network(str(n).strip(), strict=False))
        except ValueError:
            continue
    return out


def ip_in_nets(addr: str, nets) -> bool:
    try:
        ip = ipaddress.IPv4Address(str(addr).strip())
    except ValueError:
        return False
    return any(ip in n for n in _nets(nets))


def _unescape(s: str) -> str:
    """avahi-browse -p экранирует «;» и пробелы десятичными \\DDD."""
    return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), s)


def clean_name(raw: str) -> str:
    """Имя инстанса: только ASCII-набор, пробелы схлопнуты, без краевых, до 63."""
    s = _NAME_STRIP_RE.sub("", _unescape(str(raw)))
    return " ".join(s.split())[:63].strip()


def clean_host(raw: str) -> str:
    """Метка хоста в домене обзора: из «NASPi5.local» → «naspi5»; невалидная — пусто."""
    s = str(raw).strip().lower()
    if s.endswith("."):
        s = s[:-1]
    if s.endswith(".local"):
        s = s[:-6]
    s = _HOST_STRIP_RE.sub("", s.split(".")[0])[:63].strip("-")
    return s if s and _HOST_RE.fullmatch(s) else ""


def parse_avahi(text: str, own_nets) -> list[dict]:
    """Разобрать вывод `avahi-browse -rtpk _smb._tcp`: только разрешённые
    строки («=»), только IPv4 из своих подсетей, дубли по (адрес, порт) — одна
    запись, не больше MAX_OWN. Строки «+» (без адреса) и IPv6 — мимо."""
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for line in (text or "").splitlines():
        if not line.startswith("="):
            continue
        f = line.split(";")
        if len(f) < 9 or f[2] != "IPv4":
            continue
        stype, addr = f[4].strip(), f[7].strip()
        if stype not in SERVICE_TYPES or not ip_in_nets(addr, own_nets):
            continue
        try:
            port = int(f[8])
        except ValueError:
            continue
        if not 1 <= port <= 65535 or (addr, port) in seen:
            continue
        host = clean_host(_unescape(f[6])) or "h-" + addr.replace(".", "-")
        name = clean_name(f[3]) or host
        seen.add((addr, port))
        out.append({"t": stype, "n": name, "h": host, "p": port, "a": addr})
        if len(out) >= MAX_OWN:
            break
    return sorted(out, key=lambda r: (r["a"], r["p"]))


def clean(items, allowed_nets, limit: int = MAX_PEER) -> list[dict]:
    """Недоверенные записи → только те, что целиком совпали с набором: закрытые
    поля, fullmatch по шаблонам, адрес из allowed_nets, тип из списка. Не
    прошла — выбрасывается, не «чинится»; остальные живут."""
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for raw in list(items or [])[: limit * 4]:
        if not isinstance(raw, dict):
            continue
        t, n, h, p, a = (raw.get(k) for k in ("t", "n", "h", "p", "a"))
        if t not in SERVICE_TYPES or not isinstance(n, str) or not isinstance(h, str):
            continue
        if not _NAME_RE.fullmatch(n) or n != n.strip() or "  " in n:
            continue
        if not _HOST_RE.fullmatch(h) or h != h.lower():
            continue
        try:
            port = int(p)
        except (TypeError, ValueError):
            continue
        if not 1 <= port <= 65535 or not isinstance(a, str) or not ip_in_nets(a, allowed_nets):
            continue
        if (a, port) in seen:
            continue
        seen.add((a, port))
        out.append({"t": t, "n": n, "h": h, "p": port, "a": a})
        if len(out) >= limit:
            break
    return sorted(out, key=lambda r: (r["a"], r["p"]))


def feed_hash(items) -> str:
    """Отпечаток списка: sha256 канонического JSON; пустой список — пустая
    строка (пустое к пустому не едет вовсе)."""
    if not items:
        return ""
    canon = json.dumps(sorted(items, key=lambda r: (r["a"], r["p"])), sort_keys=True,
                       separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def reverse_zone(net: str) -> str:
    """Обратная зона подсети получателя, по которой Mac ищет домены обзора
    (RFC 6763 §11): адрес сети с обнулённой частью хоста, четыре октета
    наоборот — 192.168.68.0/24 → 0.68.168.192.in-addr.arpa."""
    n = ipaddress.IPv4Network(str(net).strip(), strict=False)
    return ".".join(reversed(str(n.network_address).split("."))) + ".in-addr.arpa"


def render_dnsmasq(items, own_nets, digest: str = "") -> str:
    """Файл записей для dnsmasq получателя (§3.3 концепта). Пустой список —
    пустая строка: файл снимается, а не пишется пустым."""
    items = clean(items, ["0.0.0.0/0"], MAX_PEER) if items else []
    if not items:
        return ""
    d = BROWSE_DOMAIN
    lines = ["# awg-bot (шлюз): сервисы соседних сетей. Владелец — awg-lan-services.sh.",
             f"# hash={digest or feed_hash(items)}",
             f"local=/{d}/"]
    zones = []
    for n in _nets(own_nets):
        z = reverse_zone(str(n))
        if z not in zones:
            zones.append(z)
    for z in zones:
        lines.append(f"ptr-record=b._dns-sd._udp.{z},{d}")
        lines.append(f"ptr-record=lb._dns-sd._udp.{z},{d}")
    lines.append(f"ptr-record=b._dns-sd._udp.{d},{d}")
    lines.append(f"ptr-record=lb._dns-sd._udp.{d},{d}")
    lines.append(f"ptr-record=_services._dns-sd._udp.{d},_smb._tcp.{d}")
    # метка хоста и имя инстанса уникальны: совпали у двух записей — naspi5,
    # naspi5-2 и «NAS», «NAS 2» (регистр не в счёт: dnsmasq приводит к нижнему,
    # и два одинаковых имени слились бы в один сервер с двумя целями SRV);
    # занятые (в том числе пришедшая «naspi5-2») не выдаются второй раз
    used: set[str] = set()
    used_names: set[str] = set()
    for r in items:
        base = r["h"]
        host, k = base, 1
        while host in used:
            k += 1
            host = f"{base[:60]}-{k}"
        used.add(host)
        name, k = r["n"], 1
        while name.lower() in used_names:
            k += 1
            name = f"{r['n'][:60]} {k}"
        used_names.add(name.lower())
        inst = f'"{name}._smb._tcp.{d}"'
        lines.append(f"ptr-record=_smb._tcp.{d},{inst}")
        lines.append(f"srv-host={inst},{host}.{d},{r['p']}")
        lines.append(f'txt-record={inst},""')
        lines.append(f"host-record={host}.{d},{r['a']}")
    return "\n".join(lines) + "\n"


def lines_ok(text: str) -> bool:
    """Каждая непустая строка файла совпадает с одним из LINE_RES."""
    for line in (text or "").splitlines():
        if line and not any(p.fullmatch(line) for p in LINE_RES):
            return False
    return True


def version_at_least(ver: str, floor: tuple[int, ...]) -> bool:
    """«3.1.0» ≥ (3, 1, 0)? Нечисловое — False (агент не назвал версию)."""
    try:
        parts = tuple(int(x) for x in str(ver).strip().split("."))
    except ValueError:
        return False
    return bool(parts) and parts >= floor
