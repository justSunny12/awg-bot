"""
dyndns2.py — протокол DynDNS2: No-IP, Dynu, OVH DynHost, DynDNS и подобные.

`GET https://<сервер>/nic/update?hostname=<имя>&myip=<ip>` с basic auth.
Настройка: `{provider: "dyndns2", name: "vpn.example.com", server:
"dynupdate.no-ip.com", user: "…", token: "<пароль>"}`. API чтения у протокола
нет — `read` идёт к авторитетным серверам зоны. Ответы `good`/`nochg` — успех,
остальное — отказ с кодом протокола.
"""
from __future__ import annotations

from .common import DnsError, Refused, http, ipv4, read_authoritative, refusal_status

id = "dyndns2"
human = "DynDNS2 (No-IP, Dynu, OVH DynHost…)"
# срок доставки правки на авторитетные серверы (common.apply_wait_s): цифр у провайдеров протокола
# не найдено — общий предел API-провайдеров
APPLY_S = 300
# ответы протокола «правка не принята» (явный отказ: учётная запись, имя, блокировка) — исход известен
REFUSE_WORDS = frozenset({"badauth", "nohost", "abuse", "notfqdn", "badagent", "!donator", "numhost"})


def _url(cfg: dict) -> str:
    server = str(cfg.get("server", "")).strip().strip("/")
    if not server:
        raise Refused("DynDNS2: не задан сервер обновления")      # запроса не было
    if "://" not in server:
        server = "https://" + server
    if server.endswith("/nic/update"):                       # записи прежних выпусков хранили путь в server
        server = server[:-len("/nic/update")]
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
        # коды отказа протокола и 4xx — правка не принята; 911, dnserr, 5xx, пустой ответ — исход неизвестен
        refused = (code == 200 and word in REFUSE_WORDS) or refusal_status(code)
        raise (Refused if refused else DnsError)(f"DynDNS2: ответ {text.strip()[:40] or code}")


def check(cfg: dict) -> str:
    cur = read(cfg)
    set(cfg, cur)
    return cur
