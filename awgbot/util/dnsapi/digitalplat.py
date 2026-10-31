"""
digitalplat.py — DigitalPlat DNS (кабинет DigitalPlat One, ключ `dp_live_…`
с областями `dns.read` и `dns.write`).

Настройка: `{provider: "digitalplat", name: "vpn.example.dpdns.org", token: "…"}`.
Домен в кабинете ищется по имени при каждом вызове (самый длинный суффикс
имени среди доменов ключа), запись — по имени внутри зоны (`@` — сам домен).
Запись меняется PATCH по её id с `If-Match` на отданный etag и
`Idempotency-Key`: провайдер применяет правку очередью, ответ — `operation_id`.
"""
from __future__ import annotations

import json
import uuid

from .common import DnsError, http, ipv4

id = "digitalplat"
human = "DigitalPlat"
_API = "https://domain-api.digitalplat.org/api/v1"


def _call(cfg: dict, method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:
    hdrs = {"Authorization": f"Bearer {cfg.get('token', '')}", "Accept": "application/json", **(headers or {})}
    code, text = http(method, _API + path, headers=hdrs, body=body)
    try:
        data = json.loads(text)
    except ValueError as e:
        raise DnsError(f"DigitalPlat ответил не JSON (код {code})") from e
    if not isinstance(data, dict):                    # список, строка, число — не ответ API
        raise DnsError(f"DigitalPlat ответил не объектом JSON (код {code})")
    if code // 100 != 2 or not data.get("success"):
        err = data.get("error") or {}
        why = str(err.get("message") or err.get("code") or "") if isinstance(err, dict) else str(err)
        raise DnsError(f"DigitalPlat: {why or f'код {code}'}"[:200])
    return data


def _split(cfg: dict) -> tuple[str, str]:
    """(домен в кабинете, имя записи внутри зоны): «vpn.example.dpdns.org» →
    («example.dpdns.org», «vpn»), сам домен → «@»."""
    name = str(cfg.get("name", "")).strip().strip(".").lower()
    domains = [str(d.get("domain") or d.get("name") or "").strip(".").lower()
               for d in (_call(cfg, "GET", "/domains").get("data") or []) if isinstance(d, dict)]
    best = ""
    for dom in domains:
        if dom and (name == dom or name.endswith("." + dom)) and len(dom) > len(best):
            best = dom
    if not best:
        raise DnsError(f"DigitalPlat: домен для {cfg.get('name')} не найден у этого ключа")
    rel = name[:-len(best)].rstrip(".") if name != best else ""
    return best, rel or "@"


def _record(cfg: dict) -> dict:
    dom, rel = _split(cfg)
    full = str(cfg.get("name", "")).strip().strip(".").lower()
    recs = [r for r in (_call(cfg, "GET", f"/domains/{dom}/dns/records").get("data") or [])
            if isinstance(r, dict) and str(r.get("type", "")).upper() == "A"
            and (str(r.get("fqdn") or "").strip(".").lower() == full
                 or str(r.get("name") or "@").strip(".").lower() in (rel, full))]
    if not recs:
        raise DnsError(f"DigitalPlat: A-записи {cfg.get('name')} нет")
    if len(recs) > 1:
        raise DnsError(f"DigitalPlat: у {cfg.get('name')} несколько A-записей ({len(recs)}) — оставь одну")
    rec = dict(recs[0])
    rec["_domain"] = dom
    return rec


def read(cfg: dict) -> str:
    return ipv4(_record(cfg).get("value", ""))


def set(cfg: dict, ip: str) -> None:  # noqa: A001 — имя из интерфейса провайдера
    rec = _record(cfg)
    hdrs = {"Idempotency-Key": uuid.uuid4().hex}
    if rec.get("etag"):
        hdrs["If-Match"] = str(rec["etag"])
    _call(cfg, "PATCH", f"/domains/{rec['_domain']}/dns/records/{rec['id']}",
          {"value": ipv4(ip), "ttl": int(rec.get("ttl") or 300), "disabled": False}, headers=hdrs)


def check(cfg: dict) -> str:
    return read(cfg)
