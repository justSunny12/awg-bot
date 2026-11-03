"""
duckdns.py — DuckDNS (`https://www.duckdns.org/update?domains=…&token=…&ip=…`).

Настройка: `{provider: "duckdns", name: "myvpn.duckdns.org", token: "…"}`.
API чтения у DuckDNS нет — `read` идёт к авторитетным серверам зоны.
"""
from __future__ import annotations

from .common import DnsError, Refused, http, ipv4, read_authoritative, refusal_status

id = "duckdns"
human = "DuckDNS"
# срок доставки правки на авторитетные серверы (common.apply_wait_s): официальной цифры нет,
# TTL записи — 60 с
APPLY_S = 300
_URL = "https://www.duckdns.org/update"


def _sub(cfg: dict) -> str:
    name = str(cfg.get("name", "")).strip(".").lower()
    if not name.endswith(".duckdns.org"):
        raise Refused("DuckDNS: имя должно оканчиваться на .duckdns.org")   # запроса не было
    return name[: -len(".duckdns.org")]


def read(cfg: dict) -> str:
    addrs = read_authoritative(str(cfg.get("name", "")))
    if not addrs:
        raise DnsError(f"DuckDNS: записи {cfg.get('name')} нет")
    return addrs[0]


def set(cfg: dict, ip: str) -> None:  # noqa: A001
    code, text = http("GET", f"{_URL}?domains={_sub(cfg)}&token={cfg.get('token', '')}&ip={ipv4(ip)}")
    if code != 200 or text.strip() != "OK":
        # «KO» (токен или имя не приняты) и 4xx — явный отказ; пустой ответ, 5xx — исход неизвестен
        refused = (code == 200 and text.strip() == "KO") or refusal_status(code)
        raise (Refused if refused else DnsError)(f"DuckDNS: ответ {text.strip()[:40] or code}")


def check(cfg: dict) -> str:
    """Проверка токена — обновление тем же адресом, что стоит сейчас."""
    cur = read(cfg)
    set(cfg, cur)
    return cur
