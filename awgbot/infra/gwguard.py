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
FW_ENV = "/etc/awg-gw/firewall.env"
CHAINS = ("input", "tunnel_in", "forward", "postrouting", "output")


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
    """{'sets': {имя: set(str)}, 'chains': set(имя), 'masq_ifaces': set(str)}
    или None — таблицы нет. masq_ifaces — в какие интерфейсы стоит masquerade
    (по oifname). Один exec: `nft -j list table`."""
    proc = _nft(["-j", "list", "table", TABLE_FAMILY, TABLE_NAME])
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout.decode(errors="replace") or "{}")
    except json.JSONDecodeError as e:
        raise GwGuardError(f"nft -j: {e}")
    sets: dict[str, set[str]] = {}
    chains: set[str] = set()
    masq: set[str] = set()
    for item in doc.get("nftables", []):
        if "set" in item:
            s = item["set"]
            sets[s["name"]] = {_elem_str(e) for e in (s.get("elem") or [])}
        elif "chain" in item:
            chains.add(item["chain"]["name"])
        elif "rule" in item:
            expr = item["rule"].get("expr") or []
            if any("masquerade" in e for e in expr if isinstance(e, dict)):
                for e in expr:
                    m = e.get("match") if isinstance(e, dict) else None
                    if m and (m.get("left") or {}).get("meta", {}).get("key") == "oifname":
                        masq.add(str(m.get("right")))
    return {"sets": sets, "chains": chains, "masq_ifaces": masq}


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


def unit_state() -> dict:
    """Состояние юнита обвязки: ActiveState / UnitFileState / Result. Нужно,
    чтобы отличать «таблицы нет, потому что обвязка старого образца» от «юнит
    не отработал» и «юнит выключен» — лечатся они по-разному."""
    try:
        proc = subprocess.run(["systemctl", "show", config.GW_UNIT, "--property=ActiveState",
                               "--property=UnitFileState", "--property=Result"],
                              capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for line in proc.stdout.decode(errors="replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


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
    unmarked|confirmed|foreign|unconfirmed и ключ помеченного шлюза."""
    out = {}
    try:
        for line in Path(STATUS_FILE).read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


# ── политика «Telegram → аплинк»: ip rule по метке + маршрут в таблице ──────
# Ставит PostUp аплинка при подъёме; systemd-networkd при своём (пере)запуске
# по умолчанию сносит ЧУЖИЕ ip rule и маршруты — интерфейс жив, метка стоит,
# а пакеты Telegram уходят домашнему провайдеру. Агент проверяет каждый тик и
# перевыставляет сам: это два idempotent-вызова ip, юнит дёргать незачем.

UPLINK_TABLE = 100
TG_MARK = 1


def _ip_json(args: list[str]) -> list:
    try:
        proc = subprocess.run(["ip", "-j", *args], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    try:
        return json.loads(proc.stdout.decode(errors="replace") or "[]")
    except ValueError:
        return []


def _fwmark_of(rule: dict) -> int:
    """Метка правила из `ip -j rule`: «0x1», с маской — «0x1/0xff» (чужие
    правила на той же машине); нечитаемое — 0, а не исключение на весь тик."""
    raw = str(rule.get("fwmark", "0")).split("/", 1)[0].strip()
    try:
        return int(raw, 0)
    except ValueError:
        return 0


def uplink_policy(uplink_if: str) -> dict:
    """{'rule': bool, 'route': bool} — есть ли правило «метка → таблица» и
    маршрут по умолчанию в аплинк в этой таблице."""
    rule = any(_fwmark_of(r) == TG_MARK and str(r.get("table")) == str(UPLINK_TABLE)
               for r in _ip_json(["rule", "show"]))
    route = any(r.get("dst") == "default" and r.get("dev") == uplink_if
                for r in _ip_json(["route", "show", "table", str(UPLINK_TABLE)]))
    return {"rule": rule, "route": route}


def uplink_policy_ensure(uplink_if: str, state: dict | None = None) -> list[str]:
    """Перевыставить недостающее. state — уже снятое uplink_policy (тик
    снимает его для проверок и отдаёт сюда, чтобы не ходить в ip дважды).
    Возвращает, что было восстановлено."""
    state = state if state is not None else uplink_policy(uplink_if)
    fixed: list[str] = []
    if not state["rule"]:
        if subprocess.run(["ip", "rule", "add", "fwmark", str(TG_MARK), "lookup", str(UPLINK_TABLE)],
                          capture_output=True, timeout=10).returncode == 0:
            fixed.append("правило по метке")
    if not state["route"]:
        if subprocess.run(["ip", "route", "replace", "default", "dev", uplink_if,
                           "table", str(UPLINK_TABLE)], capture_output=True, timeout=10).returncode == 0:
            fixed.append("маршрут в аплинк")
    return fixed


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


# ── локальная сеть без VPN (docs/gateway-lan.md, функция A) ──────────────────
HOME_TABLE_NAME = "awg_home"
LAN_STATUS_FILE = "/var/lib/awg-gw/lists.status"
LAN_LISTS_SCRIPT = "/usr/local/sbin/awg-lan-lists.sh"
LAN_DOMAIN_SCRIPT = "/usr/local/sbin/awg-lan-domain.sh"


def unit_env(key: str) -> str:
    """Значение Environment=KEY=… из юнита обвязки — что приехало в бандле."""
    try:
        text = Path(f"/etc/systemd/system/{config.GW_UNIT}").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(rf'^Environment="?{re.escape(key)}=([^"\n]*)"?', text, re.M)
    return (m.group(1) if m else "").strip()


def lan_mode() -> bool:
    return unit_env("LAN_MODE") == "1"


def lan_status() -> dict:
    """Что записал скрипт списков: updated_at, domains, nets, rc."""
    out = {}
    try:
        for line in Path(LAN_STATUS_FILE).read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def home_table_info() -> Optional[dict]:
    """{'sets': {имя: число элементов}, 'chains': set, 'lan_pkts': int, 'dns_pkts': int}
    или None — таблицы нет. lan_pkts — счётчик «из локальной сети наружу»
    (первое правило со счётчиком в prerouting), dns_pkts — «DNS с роутера»
    (input). Один exec."""
    proc = _nft(["-j", "list", "table", TABLE_FAMILY, HOME_TABLE_NAME])
    if proc.returncode != 0:
        return None
    try:
        doc = json.loads(proc.stdout.decode(errors="replace") or "{}")
    except json.JSONDecodeError as e:
        raise GwGuardError(f"nft -j: {e}")
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


def lan_own_lists() -> tuple[int, int]:
    """(в туннель, напрямую) — персональные списки, строки nftset=."""
    def _count(name: str) -> int:
        try:
            text = Path(f"/etc/dnsmasq.d/{name}").read_text(encoding="utf-8")
        except OSError:
            return 0
        return sum(1 for ln in text.splitlines() if ln.startswith("nftset="))
    return _count("awg-gw-vpn-user.conf"), _count("awg-gw-ru-user.conf")


def dnsmasq_active() -> Optional[bool]:
    try:
        proc = subprocess.run(["systemctl", "is-active", "--quiet", "dnsmasq"],
                              capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode == 0


def resolve_via_local(name: str = "github.com") -> Optional[bool]:
    """Резолвит ли dnsmasq через аплинк: один запрос к 127.0.0.1. None — dig нет."""
    try:
        proc = subprocess.run(["dig", "+short", "+time=3", "+tries=1", "@127.0.0.1", name, "A"],
                              capture_output=True, timeout=15)
    except FileNotFoundError:
        return None
    except (OSError, subprocess.SubprocessError):
        return False
    out = proc.stdout.decode(errors="replace")
    return proc.returncode == 0 and bool(re.search(r"^\d+\.\d+\.\d+\.\d+$", out, re.M))


def ipv6_disabled(iface: str) -> Optional[bool]:
    try:
        return Path(f"/proc/sys/net/ipv6/conf/{iface}/disable_ipv6").read_text().strip() == "1"
    except OSError:
        return None


def run_lan_lists(timeout: int = 600) -> tuple[bool, str]:
    """Обновить списки скриптом обвязки. (ok, хвост вывода)."""
    try:
        proc = subprocess.run([LAN_LISTS_SCRIPT], capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return False, "скрипта списков нет — перевыпусти конфигурацию шлюза"
    except subprocess.TimeoutExpired:
        return False, "таймаут обновления списков"
    except OSError as e:
        return False, str(e)
    tail = (proc.stdout + proc.stderr).decode(errors="replace").strip().splitlines()[-3:]
    return proc.returncode == 0, "\n".join(tail)


def run_lan_domain(cmd: str, domains: list[str], timeout: int = 60) -> tuple[bool, str]:
    """Персональные списки: add | ru | del | list. (ok, вывод)."""
    try:
        proc = subprocess.run([LAN_DOMAIN_SCRIPT, cmd, *domains], capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return False, "скрипта списков нет — перевыпусти конфигурацию шлюза"
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return proc.returncode == 0, (proc.stdout + proc.stderr).decode(errors="replace").strip()
