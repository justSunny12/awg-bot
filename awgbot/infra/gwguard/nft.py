"""nft.py — таблица защиты и наборы: чтение, синхронизация динамических наборов, таблица локальной сети."""
from __future__ import annotations

import ipaddress
import json
import re
import tempfile
from typing import Optional

from awgbot.infra import nftjson
from awgbot.infra import gwguard as pkg       # имена пакета — через него: их подменяют тесты, и подмена должна видеться везде



def table_info() -> Optional[dict]:
    """{'sets': {имя: set(str)}, 'chains': set(имя), 'masq_ifaces': set(str),
    'router_masq': set(str), 'ssh_ports': {цепочка: порт}} или None — таблицы
    нет. masq_ifaces — в какие интерфейсы стоит masquerade (по oifname);
    router_masq — к каким адресам стоит маскарад соседей (`ip saddr
    @peer_nets4 ip daddr <роутер> masquerade`); ssh_ports — какой tcp dport
    держат правила про SSH (tunnel_in: сервер по линку; input: переход на
    ssh_in). Один exec: `nft -j list table`."""
    proc = pkg._nft(["-j", "list", "table", pkg.TABLE_FAMILY, pkg.TABLE_NAME])
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout.decode(errors="replace") or "{}")
    except json.JSONDecodeError as e:
        raise pkg.GwGuardError(f"nft -j: {e}")
    sets: dict[str, set[str]] = {}
    chains: set[str] = set()
    masq: set[str] = set()
    router: set[str] = set()
    ssh_ports: dict[str, int] = {}
    for item in doc.get("nftables", []):
        if "set" in item:
            s = item["set"]
            sets[s["name"]] = {nftjson.elem_str(e) for e in (s.get("elem") or [])}
        elif "chain" in item:
            chains.add(item["chain"]["name"])
        elif "rule" in item:
            expr = item["rule"].get("expr") or []
            if any("masquerade" in e for e in expr if isinstance(e, dict)):
                ip = {}
                for e in expr:
                    m = e.get("match") if isinstance(e, dict) else None
                    if m and (m.get("left") or {}).get("meta", {}).get("key") == "oifname":
                        masq.add(str(m.get("right")))
                    pl = ((m or {}).get("left") or {}).get("payload") or {}
                    if m and pl.get("protocol") == "ip" and isinstance(m.get("right"), str):
                        ip[pl.get("field")] = m["right"]
                if ip.get("saddr") == "@peer_nets4" and ip.get("daddr"):
                    router.add(ip["daddr"])
            for e in expr:
                m = e.get("match") if isinstance(e, dict) else None
                left = (m or {}).get("left") or {}
                if m and left.get("payload", {}).get("field") == "dport" \
                        and left["payload"].get("protocol") == "tcp" and isinstance(m.get("right"), int):
                    ssh_ports[str(item["rule"].get("chain"))] = int(m["right"])
    return {"sets": sets, "chains": chains, "masq_ifaces": masq, "router_masq": router, "ssh_ports": ssh_ports}


def iptables_forward_policy() -> Optional[str]:
    """Политика чужой цепочки ip filter FORWARD: accept в нашей таблице не
    отменяет drop в ней. docker ставит DROP, если сам включал ip_forward."""
    proc = pkg._nft(["-j", "list", "chain", "ip", "filter", "FORWARD"])
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout.decode(errors="replace") or "{}")
    except json.JSONDecodeError:
        return None
    for item in doc.get("nftables", []):
        if "chain" in item:
            return str(item["chain"].get("policy") or "accept")
    return None


def set_sync(name: str, desired: set[str], info: dict | None = None) -> bool:
    """Привести живой набор таблицы к desired одной транзакцией
    (`flush set` + `add element`). Нет расхождения — ни одного exec.
    Возвращает, менял ли. Таблицы или набора нет — False (старая обвязка)."""
    if info is None:
        info = pkg.table_info()
    if not info or name not in info["sets"]:
        return False
    live = {pkg._norm_elem(e) for e in info["sets"][name]}
    want = {pkg._norm_elem(e) for e in pkg.collapse(desired)}
    if live == want:
        return False
    lines = [f"flush set {pkg.TABLE} {name}"]
    if want:
        lines.append(f"add element {pkg.TABLE} {name} {{ {', '.join(sorted(want))} }}")
    fd, tmp = tempfile.mkstemp(prefix="awg-gw-set-", suffix=".nft")
    try:
        with open(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        proc = pkg._nft(["-f", tmp])
        if proc.returncode != 0:
            raise pkg.GwGuardError("nft -f: " + proc.stderr.decode(errors="replace").strip()[-200:])
    finally:
        try:
            pkg.Path(tmp).unlink()
        except OSError:
            pass
    return True


def collapse(entries) -> list[str]:
    """Схлопнуть пересекающиеся и смежные подсети: nft отвергает пересечения
    в наборе с flags interval («conflicting intervals») — и в транзакции, и в
    файле при загрузке. Не-адреса (имена) пропускаются."""
    nets = []
    for e in entries:
        try:
            nets.append(ipaddress.ip_network(str(e), strict=False))
        except ValueError:
            continue
    return [pkg._norm_elem(str(n)) for n in ipaddress.collapse_addresses(nets)]


def overlaps(entry: str, others) -> str:
    """Чем из others пересекается entry (первое найденное), пусто — ничем.
    Для отказа «уже покрыт подсетью …» до записи в файл."""
    try:
        net = ipaddress.ip_network(str(entry), strict=False)
    except ValueError:
        return ""
    for o in others:
        try:
            on = ipaddress.ip_network(str(o), strict=False)
        except ValueError:
            continue
        if net.overlaps(on):
            return str(o)
    return ""


def lan_table_info() -> Optional[dict]:
    """{'sets': {имя: число элементов}, 'chains': set, 'lan_pkts': int, 'dns_pkts': int}
    или None — таблицы нет. lan_pkts — счётчик «из локальной сети наружу»
    (первое правило со счётчиком в prerouting), dns_pkts — «DNS с роутера»
    (input). Один exec, терсный (-t): без элементов наборов — lan_vpn4 растёт
    без конца, выгружать его каждые три минуты ради двух счётчиков незачем;
    число элементов поэтому всегда 0, счёты — из lists.status."""
    proc = pkg._nft(["-j", "-t", "list", "table", pkg.TABLE_FAMILY, pkg.LAN_TABLE_NAME])
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout.decode(errors="replace") or "{}")
    except json.JSONDecodeError as e:
        raise pkg.GwGuardError(f"nft -j: {e}")
    sets: dict[str, int] = {}
    chains: set[str] = set()
    counters: dict[str, int] = {}
    for item in doc.get("nftables", []):
        if "set" in item:
            s = item["set"]
            sets[s["name"]] = len(s.get("elem") or [])
        elif "chain" in item:
            chains.add(item["chain"]["name"])
        elif "rule" in item:
            r = item["rule"]
            chain = r.get("chain", "")
            if chain in counters:
                continue
            for e in r.get("expr") or []:
                if isinstance(e, dict) and "counter" in e:
                    counters[chain] = int((e["counter"] or {}).get("packets", 0))
                    break
    return {"sets": sets, "chains": chains,
            "lan_pkts": counters.get("prerouting", 0), "dns_pkts": counters.get("input", 0)}


def lan_table_old() -> bool:
    """Таблица локальной сети ещё под прежним именем (обвязка 3.3.0 и раньше):
    скрипт обвязки 3.3.1 переносит её на первом прогоне. Снимается со
    следующим сдвигом минимума."""
    return pkg._nft(["-t", "list", "table", pkg.TABLE_FAMILY, pkg.LAN_TABLE_OLD]).returncode == 0


def forward_accepts() -> set[str]:
    """Интерфейсы, для которых в чужой ip filter FORWARD стоят наши ACCEPT
    (раздел 3a скрипта обвязки). Пусто — не прочиталось или ничего нет."""
    try:
        proc = pkg.subprocess.run(["iptables", "-w", "-S", "FORWARD"], capture_output=True, timeout=10)
    except (OSError, pkg.subprocess.SubprocessError):
        return set()
    out = set()
    for line in proc.stdout.decode(errors="replace").splitlines():
        m = re.match(r"^-A FORWARD -(i|o) (\S+) -j ACCEPT$", line.strip())
        if m:
            out.add(f"{m.group(1)}:{m.group(2)}")
    return out
