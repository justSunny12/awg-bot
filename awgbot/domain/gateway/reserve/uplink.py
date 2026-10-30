"""
uplink.py — перерезолв аплинка шлюза: адрес для пользователей переехал — аплинк идёт
на новый адрес.

Аплинк — обычный пир интерфейса AWG сервера: его Endpoint в конфиге
— имя (`server_host`), а ядро разрешает имя один раз, при подъёме. Пока
сервер жив, этого хватает; с резервом A-запись имени переезжает на другой
хост, и аплинк со старым адресом молчал бы до перезапуска. Поэтому: нет
рукопожатия дольше порога — разрешить имя заново (потолок 3 с), адрес
другой — `awg set <аплинк> peer <ключ> endpoint <адрес>:<порт>` на живом
интерфейсе, без перезапуска. Работает и без пары — любой переезд имени.
Строка в журнале, без уведомления.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import subprocess

from awgbot.core import config
from awgbot.infra import gwguard

log = logging.getLogger(__name__)

HANDSHAKE_STALE = 180           # с без рукопожатия — проверить, не переехал ли адрес
RESOLVE_TIMEOUT = 3.0
_ENDPOINT_RE = re.compile(r"^\s*Endpoint\s*=\s*(\S+)\s*$", re.M)


def conf_endpoint(iface: str) -> tuple[str, int]:
    """(хост, порт) Endpoint пира из конфига аплинка; пусто — не прочитан."""
    path = os.path.join(config.GW_CONF_DIR, f"{iface}.conf")
    try:
        with open(path, encoding="utf-8") as f:
            m = _ENDPOINT_RE.search(f.read())
    except OSError:
        return "", 0
    if not m:
        return "", 0
    host, _, port = m.group(1).rpartition(":")
    try:
        return host.strip("[]"), int(port)
    except ValueError:
        return "", 0


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def resolve(host: str, timeout: float = RESOLVE_TIMEOUT) -> str:
    """Первый IPv4 имени; пусто — не разрешилось за потолок."""
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM)
    except (socket.gaierror, OSError):
        return ""
    finally:
        socket.setdefaulttimeout(old)
    return infos[0][4][0] if infos else ""


def set_endpoint(iface: str, pub: str, endpoint: str) -> bool:
    try:
        proc = subprocess.run(["awg", "set", iface, "peer", pub, "endpoint", endpoint], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


_IP_TOLD: set[str] = set()


def reresolve_now(name: str, expect: str, why: str = "", move: int = 0, iface: str = "") -> str:
    """Endpoint аплинка — по записи `name` у авторитетного сервера, если она даёт
    `expect` (адрес второго сервера при переезде, основы при возврате); не по
    кэшу резолвера (TTL до 300 с). Условия: Endpoint в конфиге — имя и совпадает
    с `name`. `set` | `same` | `skip:<почему>` (концепт «кворум» §4.12)."""
    from awgbot.util import dnsapi
    iface = iface or gwguard.uplink_interface()
    if not iface:
        return "skip:no-uplink"
    host, port = conf_endpoint(iface)
    if not host or not port:
        return "skip:no-endpoint"
    if is_ip(host):
        _ip_told(iface, host)
        return "skip:ip"
    if not name or host.lower().rstrip(".") != str(name).lower().rstrip("."):
        return "skip:other-name"
    ip = expect if is_ip(expect) else resolve(expect)
    if not ip:
        return "skip:unresolved"
    try:
        addrs = dnsapi.read_authoritative(name)
    except dnsapi.DnsError as e:
        log.info("аплинк %s: запись %s у авторитетного сервера не прочитана: %s", iface, name, e)
        return "skip:unread"
    if ip not in addrs:
        return "skip:record"
    if gwguard._endpoint_host(iface) == ip:
        return "same"
    pub = (gwguard._awg(["show", iface, "peers"]).split() or [""])[0]
    if not pub:
        return "skip:no-peer"
    if not set_endpoint(iface, pub, f"{ip}:{port}"):
        log.warning("аплинк %s: Endpoint %s:%s по записи не применился", iface, ip, port)
        return "skip:set-failed"
    log.info("аплинк %s: Endpoint по записи у авторитетного сервера — %s (%s, move %s)", iface, ip, why or "сразу", move)
    return "set"


def _ip_told(iface: str, host: str) -> None:
    """Endpoint — адрес: строка журнала один раз за запуск (§4.12)."""
    if iface in _IP_TOLD:
        return
    _IP_TOLD.add(iface)
    log.warning("аплинк %s: Endpoint — адрес %s, за переездом пользователей не пойдёт", iface, host)


def reresolve(iface: str = "", stale: int = HANDSHAKE_STALE) -> str:
    """Один проход: `fresh` — рукопожатие свежее; `same` — адрес тот же;
    `set` — Endpoint переставлен; `skip:<почему>` — не к чему."""
    iface = iface or gwguard.uplink_interface()
    if not iface:
        return "skip:no-uplink"
    state, age = gwguard.handshake_state(iface)
    if state == "ok" and age is not None and age < stale:
        return "fresh"
    if state in ("absent", "unknown"):
        return f"skip:{state}"
    host, port = conf_endpoint(iface)
    if not host or not port:
        return "skip:no-endpoint"
    if is_ip(host):
        _ip_told(iface, host)
        return "skip:ip"                      # адрес в конфиге — IP: переезжать некуда
    ip = resolve(host)
    if not ip:
        return "skip:unresolved"
    if gwguard._endpoint_host(iface) == ip:
        return "same"
    pub = (gwguard._awg(["show", iface, "peers"]).split() or [""])[0]
    if not pub:
        return "skip:no-peer"
    if not set_endpoint(iface, pub, f"{ip}:{port}"):
        log.warning("аплинк %s: адрес %s → %s не применился", iface, host, ip)
        return "skip:set-failed"
    log.info("аплинк %s: адрес %s переехал — Endpoint %s:%s", iface, host, ip, port)
    return "set"
