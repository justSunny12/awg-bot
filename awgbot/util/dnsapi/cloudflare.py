"""
cloudflare.py — Cloudflare DNS API (токен с правом `Zone.DNS: Edit` на одну зону).

Настройка: `{provider: "cloudflare", name: "vpn.example.com", token: "…"}`.
Зона и запись ищутся по имени при каждом вызове: id не хранятся, чтобы
перенос зоны или пересоздание записи не ломали смену.
"""
from __future__ import annotations

import json

from .common import DnsError, http, ipv4

id = "cloudflare"
human = "Cloudflare"
_API = "https://api.cloudflare.com/client/v4"


def _call(cfg: dict, method: str, path: str, body: dict | None = None) -> dict:
    code, text = http(method, _API + path, headers={"Authorization": f"Bearer {cfg.get('token', '')}"}, body=body)
    try:
        data = json.loads(text)
    except ValueError as e:
        raise DnsError(f"Cloudflare ответил не JSON (код {code})") from e
    if not isinstance(data, dict):                    # список, строка, число — не ответ API
        raise DnsError(f"Cloudflare ответил не объектом JSON (код {code})")
    if code != 200 or not data.get("success"):
        errors = data.get("errors") if isinstance(data.get("errors"), list) else []
        errs = "; ".join(str(e.get("message", e) if isinstance(e, dict) else e) for e in errors[:2]) or f"код {code}"
        raise DnsError(f"Cloudflare: {errs}"[:200])
    return data


def _zone_id(cfg: dict) -> str:
    labels = str(cfg.get("name", "")).strip(".").split(".")
    for i in range(len(labels) - 1):
        zone = ".".join(labels[i:])
        res = _call(cfg, "GET", f"/zones?name={zone}&status=active").get("result") or []
        if res:
            return str(res[0]["id"])
    raise DnsError(f"Cloudflare: зона для {cfg.get('name')} не найдена у этого токена")


def _record(cfg: dict) -> dict:
    zid = _zone_id(cfg)
    res = _call(cfg, "GET", f"/zones/{zid}/dns_records?type=A&name={cfg.get('name')}").get("result") or []
    if not res:
        raise DnsError(f"Cloudflare: A-записи {cfg.get('name')} нет")
    if len(res) > 1:
        raise DnsError(f"Cloudflare: у {cfg.get('name')} несколько A-записей ({len(res)}) — оставь одну")
    rec = dict(res[0])
    rec["_zone"] = zid
    return rec


def read(cfg: dict) -> str:
    return ipv4(_record(cfg).get("content", ""))


def set(cfg: dict, ip: str) -> None:  # noqa: A001 — имя из интерфейса провайдера
    rec = _record(cfg)
    _call(cfg, "PATCH", f"/zones/{rec['_zone']}/dns_records/{rec['id']}",
          {"content": ipv4(ip), "ttl": int(rec.get("ttl") or 60), "proxied": False})


def check(cfg: dict) -> str:
    return read(cfg)
