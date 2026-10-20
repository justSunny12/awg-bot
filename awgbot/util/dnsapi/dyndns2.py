"""
dyndns2.py — протокол DynDNS2: No-IP, Dynu, OVH DynHost, DynDNS и подобные.

`GET https://<сервер>/nic/update?hostname=<имя>&myip=<ip>` с basic auth.
Настройка: `{provider: "dyndns2", name: "vpn.example.com", server:
"dynupdate.no-ip.com", user: "…", token: "<пароль>"}`. API чтения у протокола
нет — `read` идёт к авторитетным серверам зоны. Ответы `good`/`nochg` — успех,
остальное — отказ с кодом протокола.
"""
from __future__ import annotations

from .common import DnsError, http, ipv4, read_authoritative

id = "dyndns2"
human = "DynDNS2 (No-IP, Dynu, OVH DynHost…)"


def _url(cfg: dict) -> str:
    server = str(cfg.get("server", "")).strip().strip("/")
    if not server:
        raise DnsError("DynDNS2: не задан сервер обновления")
    if "://" not in server:
        server = "https://" + server
    return f"{server}/nic/update"


def read(cfg: dict) -> str:
    addrs = read_authoritative(str(cfg.get("name", "")))
    if not addrs:
        raise DnsError(f"DynDNS2: записи {cfg.get('name')} нет")
    return addrs[0]


def set(cfg: dict, ip: str) -> None:  # noqa: A001
    code, text = http("GET", f"{_url(cfg)}?hostname={cfg.get('name', '')}&myip={ipv4(ip)}",
                      auth=(str(cfg.get("user", "")), str(cfg.get("token", ""))))
    word = text.strip().split()[0] if text.strip() else ""
    if code != 200 or word not in ("good", "nochg"):
        raise DnsError(f"DynDNS2: ответ {text.strip()[:40] or code}")


def check(cfg: dict) -> str:
    cur = read(cfg)
    set(cfg, cur)
    return cur
