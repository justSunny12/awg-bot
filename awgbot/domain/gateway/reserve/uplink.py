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

Адрес, поставленный по авторитетной записи (`reresolve_now`), «приколот» на
TTL записи (не меньше 300 с): кэш системного резолвера до тех пор может
отдавать прежний адрес, и периодический проход вернул бы аплинк на него.
Прикол лежит на диске агента (`/var/lib/awg-gw/uplink-pin.json`, 0600: адрес и
срок по часам стены): перезапуск агента или шлюза в окно TTL его не теряет.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

from awgbot.core import config
from awgbot.infra import gwguard
from awgbot.util import fsatomic

log = logging.getLogger(__name__)

HANDSHAKE_STALE = 180           # с без рукопожатия — проверить, не переехал ли адрес
PIN_MIN = 300                   # с: не меньше — прикол адреса по записи держится до TTL кэша
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
PIN_PATH = Path("/var/lib/awg-gw/uplink-pin.json")
_PIN: dict[str, tuple[str, float, float]] | None = None   # аплинк → (адрес по записи, до time.time(), срок); None — не прочитан
_PIN_LOCK = threading.Lock()                # проход планировщика и слово резерва — разные потоки
_PIN_TOLD: set[tuple[str, str]] = set()     # (аплинк, адрес кэша) — строка журнала один раз


def _pins() -> dict[str, tuple[str, float, float]]:
    """Приколы — с диска при первом обращении за запуск (под `_PIN_LOCK`)."""
    global _PIN
    if _PIN is None:
        _PIN = {}
        try:
            data = json.loads(PIN_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        for iface, v in (data.items() if isinstance(data, dict) else ()):
            try:
                ip, until, span = str(v["ip"]), float(v["until"]), float(v["span"])
            except (KeyError, TypeError, ValueError):
                continue
            if is_ip(ip):
                _PIN[str(iface)] = (ip, until, span)
    return _PIN


def _pins_save(pins: dict[str, tuple[str, float, float]]) -> None:
    try:
        fsatomic.write_private(PIN_PATH, json.dumps(
            {k: {"ip": ip, "until": until, "span": span} for k, (ip, until, span) in pins.items()}))
    except OSError as e:
        log.warning("аплинк: прикол адреса по записи не записан на диск (%s) — держится до перезапуска агента", e)


def _pin(iface: str, ip: str, ttl: int) -> None:
    """Адрес подтверждён записью у авторитетного сервера — кэш резолвера с
    прежним ответом живёт до TTL; до тех пор периодический проход его не ставит."""
    span = float(max(int(ttl or 0), PIN_MIN))
    with _PIN_LOCK:
        pins = _pins()
        pins[iface] = (ip, time.time() + span, span)
        _pins_save(pins)
        _PIN_TOLD.clear()


def _pinned(iface: str, ip: str) -> bool:
    """Кэш даёт `ip`, а аплинк приколот к другому адресу по записи — не трогать."""
    with _PIN_LOCK:
        pins = _pins()
        pin = pins.get(iface)
        if not pin:
            return False
        left = pin[1] - time.time()
        if left <= 0 or left > pin[2]:        # срок вышел; больше срока — часы ушли назад, срок не доверен
            pins.pop(iface, None)
            _pins_save(pins)
            return False
        if ip == pin[0]:
            return False
        if (iface, ip) in _PIN_TOLD:
            return True
        _PIN_TOLD.add((iface, ip))
    log.info("аплинк %s: кэш резолвера даёт %s, а запись у авторитетного сервера — %s; адрес не меняю до истечения TTL",
             iface, ip, pin[0])
    return True


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
        addrs, ttl = dnsapi.read_authoritative_ttl(name)
    except dnsapi.DnsError as e:
        log.info("аплинк %s: запись %s у авторитетного сервера не прочитана: %s", iface, name, e)
        return "skip:unread"
    if ip not in addrs:
        return "skip:record"
    if gwguard._endpoint_host(iface) == ip:
        _pin(iface, ip, ttl)
        return "same"
    pub = (gwguard._awg(["show", iface, "peers"]).split() or [""])[0]
    if not pub:
        return "skip:no-peer"
    if not set_endpoint(iface, pub, f"{ip}:{port}"):
        log.warning("аплинк %s: Endpoint %s:%s по записи не применился", iface, ip, port)
        return "skip:set-failed"
    _pin(iface, ip, ttl)
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
    if _pinned(iface, ip):
        return "skip:pinned"
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
