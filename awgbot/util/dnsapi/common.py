"""
common.py — общее для провайдеров: интерфейс, HTTP, чтение записи.

Чтение у авторитетного сервера — свой маленький DNS-клиент по UDP (без
зависимостей): по системному резолверу находятся NS зоны (имя укорачивается
по меткам, пока у кого-то не найдутся NS), их адреса — через `getaddrinfo`,
затем A-запрос имени прямо у одного из них. Так факт смены записи виден без
ожидания кэшей: TTL записи здесь ни при чём.
"""
from __future__ import annotations

import ipaddress
import json
import random
import socket
import struct
import urllib.error
import urllib.request
from typing import Protocol

from awgbot.util import net4

TIMEOUT = 3.0
_IPV4 = ipaddress.IPv4Address


class DnsError(RuntimeError):
    """Провайдер или сеть ответили не тем, чем ждали; текст — человеку."""


class NotSupported(DnsError):
    """Действие у этого провайдера невозможно (режим «вручную»)."""


class Provider(Protocol):
    id: str
    human: str

    def read(self, cfg: dict) -> str:
        """Текущее значение записи по API провайдера (IPv4 строкой)."""

    def set(self, cfg: dict, ip: str) -> None:
        """Сменить запись на `ip`; успех — без исключения."""

    def check(self, cfg: dict) -> str:
        """Проверить настройку (токен, имя): вернуть текущее значение или DnsError."""


# ── HTTP ──────────────────────────────────────────────────────────────────────

def http(method: str, url: str, *, headers: dict | None = None, body: dict | None = None,
         timeout: float = TIMEOUT, auth: tuple[str, str] | None = None) -> tuple[int, str]:
    """Один запрос: (код, тело). Без ответа HTTP — DnsError."""
    hdrs = {"User-Agent": "awg-bot", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    if auth is not None:
        import base64
        hdrs["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with net4.urlopen(req, timeout=timeout) as resp:              # только IPv4
            return int(resp.status), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return int(e.code), e.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 — DNS, TLS, таймаут
        raise DnsError(f"провайдер DNS не ответил: {str(e) or e.__class__.__name__}"[:200]) from e


def ipv4(text: str) -> str:
    """Строка — IPv4, иначе DnsError."""
    try:
        return str(_IPV4(str(text or "").strip()))
    except ValueError as e:
        raise DnsError(f"в записи не IPv4: {text!r}"[:120]) from e


# ── чтение записи ─────────────────────────────────────────────────────────────

def read_system(name: str, timeout: float = TIMEOUT) -> list[str]:
    """A-адреса имени через системный резолвер (с кэшем, как у пользователей)."""
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        infos = socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        return []
    finally:
        socket.setdefaulttimeout(old)
    return sorted({i[4][0] for i in infos})


def _qname(name: str) -> bytes:
    out = b""
    labels = name.strip(".").split(".")
    if not name.strip(".") or len(name) > 253:
        raise DnsError(f"имя {name!r} не годится для DNS")
    for label in labels:
        if not label or len(label) > 63:
            raise DnsError(f"имя {name!r} не годится для DNS")
        try:
            raw = label.encode("idna")
        except UnicodeError as e:
            raise DnsError(f"имя {name!r} не годится для DNS") from e
        if not raw or len(raw) > 63:
            raise DnsError(f"имя {name!r} не годится для DNS")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def _read_name(msg: bytes, off: int) -> tuple[str, int]:
    labels, jumped, end = [], False, off
    for _ in range(128):
        ln = msg[off]
        if ln == 0:
            off += 1
            break
        if ln & 0xC0 == 0xC0:
            ptr = struct.unpack(">H", msg[off:off + 2])[0] & 0x3FFF
            if not jumped:
                end = off + 2
            off, jumped = ptr, True
            continue
        labels.append(msg[off + 1:off + 1 + ln].decode("ascii", errors="replace"))
        off += 1 + ln
    if not jumped:
        end = off
    return ".".join(labels), end


def query(server: str, name: str, qtype: int, timeout: float = TIMEOUT) -> list[tuple[str, int, bytes, bytes, int, int]]:
    """Один UDP-запрос: список записей (имя, тип, rdata, сообщение, смещение
    rdata в сообщении, TTL) из ответа и раздела authority. Нет ответа — DnsError."""
    qid = random.randrange(65536)
    msg = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + _qname(name) + struct.pack(">HH", qtype, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(msg, (server, 53))
        data, _ = s.recvfrom(4096)
    except OSError as e:
        raise DnsError(f"сервер DNS {server} не ответил: {e}") from e
    finally:
        s.close()
    if len(data) < 12 or struct.unpack(">H", data[:2])[0] != qid:
        raise DnsError(f"сервер DNS {server} ответил не на наш запрос")
    _, flags, qd, an, ns, _ar = struct.unpack(">HHHHHH", data[:12])
    if flags & 0x000F not in (0, 3):             # NXDOMAIN — ответ без записей, остальное — ошибка
        raise DnsError(f"сервер DNS {server}: код ошибки {flags & 0xF}")
    try:
        return _records(data, qd, an + ns)
    except (IndexError, struct.error) as e:      # ответ оборван или искажён — не наша ошибка, но и не падение
        raise DnsError(f"сервер DNS {server} ответил обрывком") from e


def _records(data: bytes, qd: int, n: int) -> list[tuple[str, int, bytes, bytes, int, int]]:
    off = 12
    for _ in range(qd):
        _, off = _read_name(data, off)
        off += 4
    out = []
    for _ in range(n):
        nm, off = _read_name(data, off)
        rtype, _, ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
        off += 10
        if off + rdlen > len(data):
            raise IndexError("rdata за концом ответа")
        out.append((nm, rtype, data[off:off + rdlen], data, off, int(ttl)))
        off += rdlen
    return out


def _ns_of(name: str, timeout: float) -> list[str]:
    """Имена NS ближайшей зоны: спрашиваем системный резолвер, укорачивая имя."""
    resolvers = _system_resolvers()
    if not resolvers:
        raise DnsError("не найден системный резолвер (/etc/resolv.conf)")
    labels = name.strip(".").split(".")
    for i in range(len(labels) - 1):
        zone = ".".join(labels[i:])
        for srv in resolvers:
            try:
                recs = query(srv, zone, 2, timeout)
            except DnsError:
                continue
            names = [_read_name(msg, at)[0] for nm, t, rdata, msg, at, _ttl in recs if t == 2]
            if names:
                return names
            break
    raise DnsError(f"авторитетные серверы зоны для {name} не нашлись")


def _system_resolvers() -> list[str]:
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as f:
            return [l.split()[1] for l in f if l.startswith("nameserver") and len(l.split()) > 1][:3]
    except OSError:
        return []


def read_authoritative(name: str, timeout: float = TIMEOUT) -> list[str]:
    """A-адреса имени у авторитетного сервера зоны — факт записи без кэшей.
    Пустой список — записи нет; недоступность — DnsError."""
    return read_authoritative_ttl(name, timeout)[0]


def read_authoritative_ttl(name: str, timeout: float = TIMEOUT) -> tuple[list[str], int]:
    """То же, с TTL A-записи (наименьший из ответа; 0 — записей нет): по нему
    бот предупреждает админа и выдерживает паузу перед «переподключи VPN»."""
    last: DnsError | None = None
    for ns in _ns_of(name, timeout):
        for addr in read_system(ns, timeout)[:2]:
            try:
                recs = query(addr, name, 1, timeout)
            except DnsError as e:
                last = e
                continue
            a_recs = [(rdata, ttl) for nm, t, rdata, _m, _a, ttl in recs if t == 1 and len(rdata) == 4]
            addrs = sorted({str(_IPV4(rdata)) for rdata, _t in a_recs})
            return addrs, (min(ttl for _r, ttl in a_recs) if a_recs else 0)
    raise last or DnsError(f"авторитетные серверы {name} не ответили")


# ── провайдеры ────────────────────────────────────────────────────────────────

def provider_for(cfg: dict) -> Provider:
    """Провайдер по настройке; незнакомый — DnsError."""
    pid = str((cfg or {}).get("provider") or "").strip().lower()
    mod = PROVIDERS.get(pid)
    if mod is None:
        raise DnsError(f"провайдер DNS {pid!r} неизвестен")
    return mod


def _load() -> dict:
    from . import cloudflare, digitalplat, duckdns, dyndns2, manual
    return {m.id: m for m in (cloudflare, digitalplat, duckdns, dyndns2, manual)}


PROVIDERS: dict = {}
PROVIDERS.update(_load())
