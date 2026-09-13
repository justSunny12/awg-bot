"""
gwguard.py — сторона ШЛЮЗА: чтение таблицы `inet awg_gw_guard`, которую ставит
routing-gw-setup.sh (юнит awg-link-gw.service), и локальные добавки к ней.

Генератор таблицы — ОДИН, скрипт: он же реассертит её при загрузке и по
`systemctl restart awg-link-gw.service`. Агент таблицу не пишет — только
читает (проверки, панель) и просит перевыставить. Единственное, что живёт на
самом шлюзе, — ADMIN_IPS_EXTRA в /etc/awg-gw/firewall.env: адреса, добавленные
командой `awg-bot firewall allow` сверх приехавших в бандле устройств админа
(им с туннеля открыто всё: сама машина и домашняя сеть за ней).
"""
from __future__ import annotations

import ipaddress
import json
import re
import subprocess
from pathlib import Path
from typing import Optional

from awgbot.core import config

TABLE_FAMILY = "inet"
TABLE_NAME = "awg_gw_guard"
TABLE = f"{TABLE_FAMILY} {TABLE_NAME}"
GUARD_FILE = "/etc/awg-gw/guard.nft"
FW_ENV = "/etc/awg-gw/firewall.env"
CHAINS = ("input", "tunnel_in", "forward", "postrouting", "output")
SETS = ("tunnel_nets4", "private4", "tg_nets4", "admin4")


class GwGuardError(RuntimeError):
    pass


def _nft(args: list[str], timeout: int = 10) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["nft", *args], capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise GwGuardError("nft не найден — установите пакет nftables")
    except subprocess.TimeoutExpired:
        raise GwGuardError("таймаут nft")


def _elem_str(el) -> str:
    if isinstance(el, str):
        return el
    if isinstance(el, dict):
        if "prefix" in el:
            return f"{el['prefix']['addr']}/{el['prefix']['len']}"
        if "elem" in el:
            return _elem_str(el["elem"].get("val"))
        if "val" in el:
            return _elem_str(el["val"])
    return str(el)


def table_info() -> Optional[dict]:
    """{'sets': {имя: set(str)}, 'chains': set(имя)} или None — таблицы нет.
    Один exec: `nft -j list table`."""
    proc = _nft(["-j", "list", "table", TABLE_FAMILY, TABLE_NAME])
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout.decode(errors="replace") or "{}")
    except json.JSONDecodeError as e:
        raise GwGuardError(f"nft -j: {e}")
    sets: dict[str, set[str]] = {}
    chains: set[str] = set()
    for item in doc.get("nftables", []):
        if "set" in item:
            s = item["set"]
            sets[s["name"]] = {_elem_str(e) for e in (s.get("elem") or [])}
        elif "chain" in item:
            chains.add(item["chain"]["name"])
    return {"sets": sets, "chains": chains}


def iptables_forward_policy() -> Optional[str]:
    """Политика чужой цепочки ip filter FORWARD: accept в нашей таблице не
    отменяет drop в ней. docker ставит DROP, если сам включал ip_forward."""
    proc = _nft(["-j", "list", "chain", "ip", "filter", "FORWARD"])
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


# ── локальные добавки: ADMIN_IPS_EXTRA ───────────────────────────────────────

def read_extra() -> list[str]:
    try:
        text = Path(FW_ENV).read_text(encoding="utf-8")
    except OSError:
        return []
    m = re.search(r'^ADMIN_IPS_EXTRA="([^"\n]*)"', text, re.M)
    return [t for t in (m.group(1).split() if m else []) if t]


def write_extra(entries: list[str]) -> None:
    for e in entries:
        ipaddress.ip_network(e, strict=False)          # ValueError наружу
    p = Path(FW_ENV)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = ("# awg-bot (шлюз): доверенные адреса сверх устройств админа из бандла —\n"
            "# им с туннеля открыт шлюз и домашняя сеть. Правится командой\n"
            "# awg-bot firewall allow/deny; читает юнит awg-link-gw.\n"
            f'ADMIN_IPS_EXTRA="{" ".join(entries)}"\n')
    tmp = p.with_suffix(".env.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(p)


def unit_admin_ips() -> list[str]:
    """ADMIN_IPS из юнита — что приехало в бандле (устройства админа)."""
    try:
        text = Path(f"/etc/systemd/system/{config.GW_UNIT}").read_text(encoding="utf-8")
    except OSError:
        return []
    m = re.search(r'^Environment="?ADMIN_IPS=([^"\n]*)"?', text, re.M)
    return [t for t in (m.group(1).split() if m else []) if t]


def reassert() -> tuple[bool, str]:
    """Перевыставить таблицу: рестарт юнита — тот зовёт скрипт с окружением
    бандла. Линк скрипт не трогает, если конфиг не менялся."""
    proc = subprocess.run(["systemctl", "restart", config.GW_UNIT],
                          capture_output=True, timeout=90)
    ok = proc.returncode == 0
    return ok, "" if ok else proc.stderr.decode(errors="replace").strip()[-300:]


# ── шлюзовое устройство: аплинк этой машины и решение скрипта ────────────────
STATUS_FILE = "/etc/awg-gw/gateway.status"


def _awg(args: list[str]) -> str:
    try:
        proc = subprocess.run(["awg", *args], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.decode(errors="replace").strip() if proc.returncode == 0 else ""


def _endpoint_host(iface: str) -> str:
    """Хост Endpoint первого пира интерфейса (`awg show <if> endpoints`)."""
    out = _awg(["show", iface, "endpoints"])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] != "(none)":
            return parts[1].rsplit(":", 1)[0].strip("[]")
    return ""


def uplink_interface() -> str:
    """Клиентский туннель к ВПС. Задан в conf — он; иначе не-линк интерфейс с
    тем же хостом Endpoint, что у линка (тот же ВПС), иначе единственный
    не-линк интерфейс."""
    if config.GW_UPLINK_IF:
        return config.GW_UPLINK_IF
    names = [n for n in _awg(["show", "interfaces"]).split() if n != config.GW_LINK_IF]
    link_host = _endpoint_host(config.GW_LINK_IF)
    same = [n for n in names if link_host and _endpoint_host(n) == link_host]
    if len(same) == 1:
        return same[0]
    if len(names) == 1:
        return names[0]
    return ""


def uplink_pubkey() -> tuple[str, str]:
    """(интерфейс, публичный ключ) аплинка; пусто — не нашли."""
    iface = uplink_interface()
    if not iface:
        return "", ""
    return iface, _awg(["show", iface, "public-key"])


def script_status() -> dict:
    """Что решил скрипт обвязки при последнем применении: GW_STATUS
    unmarked|confirmed|foreign и ключ помеченного шлюза."""
    out = {}
    try:
        for line in Path(STATUS_FILE).read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def client_subnet() -> str:
    """Подсеть клиентов ВПС: из conf агента, иначе из юнита обвязки, куда её
    вшил бандл. Установщику спрашивать её незачем."""
    if config.GW_CLIENT_SUBNET:
        return config.GW_CLIENT_SUBNET
    try:
        text = Path(f"/etc/systemd/system/{config.GW_UNIT}").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r'^Environment="?CLIENT_SUBNET=([0-9./]+)"?', text, re.M)
    return m.group(1) if m else ""
