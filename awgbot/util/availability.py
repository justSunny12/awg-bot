"""
availability.py — доступность чего-либо за последние сутки, в процентах замеров.

Замеры складываются в ЧАСОВЫЕ корзины в state (JSON {час: [ок, всего]}):
тик живости на ВПС ходит каждые полминуты, и хранить 2880 точек ради одного
процента незачем. Корзины старше суток выбрасываются при каждой записи.
"""
from __future__ import annotations

import json

from awgbot.util import timeutil

_KEEP_HOURS = 25            # 24 полных часа + текущий неполный


def record(db, key: str, ok: bool, now=None) -> None:
    now = now or timeutil.now()
    hour = now.strftime("%Y-%m-%dT%H")
    try:
        buckets = json.loads(db.get_state(key) or "{}")
    except (json.JSONDecodeError, ValueError):
        buckets = {}
    cur = buckets.get(hour) or [0, 0]
    buckets[hour] = [cur[0] + (1 if ok else 0), cur[1] + 1]
    keep = sorted(buckets)[-_KEEP_HOURS:]
    db.set_state(key, json.dumps({h: buckets[h] for h in keep}))


def percent(db, key: str, now=None, hours: int = 24) -> float | None:
    """Процент удачных замеров за последние hours часов; None — замеров нет."""
    now = now or timeutil.now()
    try:
        buckets = json.loads(db.get_state(key) or "{}")
    except (json.JSONDecodeError, ValueError):
        return None
    import datetime as dt
    floor = (now - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H")
    ok = total = 0
    for h, (o, t) in buckets.items():
        if h >= floor:
            ok += int(o); total += int(t)
    if not total:
        return None
    return round(100.0 * ok / total, 1)
