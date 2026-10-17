"""report.py — часть пакета merge (см. __init__): итог слияния для
`awg-bot restore` и для отчёта бота после рестарта — по каждому слоту,
устройства, которые не встали, адреса для DROP до подъёма интерфейсов."""
from __future__ import annotations

import sqlite3

from .. import plan as _plan

REPORT_NAME = "restore-merge.json"


def blocked_addresses(db_path: str) -> list[str]:
    """Адреса с любой причиной блокировки в собранной базе: DROP на них стоит
    до подъёма интерфейсов и повторяется после — доступа без причины нет ни
    секунды."""
    con = sqlite3.connect(db_path)
    try:
        return [r[0] for r in con.execute("SELECT address FROM devices WHERE block_reason != 0 ORDER BY id")]
    finally:
        con.close()


def build_report(p, db_path: str, lost: list[dict], rechecked: dict) -> dict:
    return {"line": p.line, "empty": p.empty, "created_at": p.created_at,
            "slots": [{"slot": s.slot, "case": s.case, "name": s.name, "label": s.label, "link_if": s.link_if}
                      for s in p.slots],
            "lost": [{k: d.get(k, "") for k in ("device", "client", "address")} for d in lost],
            "blocked": blocked_addresses(db_path),
            "adopt": [s.link_if for s in p.slots if s.case == _plan.C_RESTORED and s.link_if],
            "expired": int((rechecked or {}).get("expired") or 0)}
