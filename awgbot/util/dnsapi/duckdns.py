"""
duckdns.py — DuckDNS (`https://www.duckdns.org/update?domains=…&token=…&ip=…`).

Настройка: `{provider: "duckdns", name: "myvpn.duckdns.org", token: "…"}`.
API чтения у DuckDNS нет — `read` идёт к авторитетным серверам зоны.
"""
from __future__ import annotations

from .common import DnsError, http, ipv4, read_authoritative

id = "duckdns"
human = "DuckDNS"
_URL = "https://www.duckdns.org/update"


def _sub(cfg: dict) -> str:
    name = str(cfg.get("name", "")).strip(".").lower()
    if not name.endswith(".duckdns.org"):
        raise DnsError("DuckDNS: имя должно оканчиваться на .duckdns.org")
    return name[: -len(".duckdns.org")]


def read(cfg: dict) -> str:
    addrs = read_authoritative(str(cfg.get("name", "")))
    if not addrs:
        raise DnsError(f"DuckDNS: записи {cfg.get('name')} нет")
    return addrs[0]


def set(cfg: dict, ip: str) -> None:  # noqa: A001
    code, text = http("GET", f"{_URL}?domains={_sub(cfg)}&token={cfg.get('token', '')}&ip={ipv4(ip)}")
    if code != 200 or text.strip() != "OK":
        raise DnsError(f"DuckDNS: ответ {text.strip()[:40] or code}")


def check(cfg: dict) -> str:
    """Проверка токена — обновление тем же адресом, что стоит сейчас."""
    cur = read(cfg)
    set(cfg, cur)
    return cur
