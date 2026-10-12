"""lists.py — часть пакета admin (см. __init__): списки онлайн, истекающих, без профиля и трафик списком."""

from __future__ import annotations

from awgbot.util import timeutil
from awgbot.bot import ui
from ..fmt import (
    _e, human_bytes, device_state, plain_ip, rf_value, tree, sub_line, profile_link,
    admin_device_link, _n_devices, _BYTES_PER_GB)
from ..fmt import deep_link as _deep_link
from ..routing import ROUTING_NAME
from .common import TRAFFIC_PAYLOAD, month_label, _total, _LIST_SEP, _LIST_CAP, _more


# ─────────────────────────────────────────────────────────────────────────────
# Списки: онлайн, истекающие, без профиля
# ─────────────────────────────────────────────────────────────────────────────


def online_devices_text(rows, bot_username: str = "") -> str:
    """«📶 Онлайн: 3 (7 устройств)» — профили и устройства, как на главной;
    записи через пустую строку, шлюзы — вверху, имя устройства — ссылка на
    карточку, имя профиля — на карточку профиля; устройства без профиля (и
    служебного) — «без профиля». rows — [(устройство, профиль или None)]."""
    profiles = len({c.id for _d, c in rows if c is not None and not getattr(c, "is_service", 0)})
    head = ui.head("📶 Онлайн", value=f"{profiles} ({_n_devices(len(rows))})" if rows else "0")
    if not rows:
        return head + _LIST_SEP + "Сейчас никто не подключён"
    items = []
    for d, c in rows[:_LIST_CAP]:
        if getattr(d, "is_gateway", 0):
            items.append(f"🛰 {admin_device_link(d, bot_username)} [шлюз] · {plain_ip(d.address)}")
            continue
        who = ("без профиля" if c is None or getattr(c, "is_service", 0)
               else profile_link(c, bot_username))
        items.append(f"{device_state(d, for_admin=True)} {admin_device_link(d, bot_username)} · "
                     f"{who} · {plain_ip(d.address)}")
    return head + _LIST_SEP + _LIST_SEP.join(items) + _more(len(rows))


def expiring_text(rows, bot_username: str = "") -> str:
    """«⏳ Истекают: 2»; «[Ксюша] — 3 дн., до 27.09 18:00»."""
    head = ui.head("⏳ Истекают", value=str(len(rows)))
    if not rows:
        return head + _LIST_SEP + "Истекающих подписок нет"
    items = []
    for c, _secs in rows[:_LIST_CAP]:
        end = timeutil.parse_iso(c.period_end)
        items.append(f"{profile_link(c, bot_username)} — {timeutil.remaining_brief(end)}, "
                     f"до {timeutil.fmt_end_ui(end)}")
    return head + _LIST_SEP + _LIST_SEP.join(items) + _more(len(rows))


def unassigned_text(n: int) -> str:
    if not n:
        return ui.head("📦 Без профиля", value="никого")
    return ui.head("📦 Без профиля", value=f"{n} — {'пир создан' if n == 1 else 'пиры созданы'} мимо бота")


# ─────────────────────────────────────────────────────────────────────────────
# Трафик списком (общий трафик ведущий, РФ — вложенной строкой под записью)
# ─────────────────────────────────────────────────────────────────────────────

_OUTSIDE = "🧐 Вне профилей: {v} — удалённые устройства и первые минуты новых"


def _rf_sub(rf) -> list[str]:
    """Вложенная строка РФ под записью — только при ненулевой РФ-части."""
    if not rf or int(rf[0]) + int(rf[1]) <= 0:
        return []
    return [f"🇷🇺 {ROUTING_NAME}: {human_bytes(int(rf[0]) + int(rf[1]))}"]


def traffic_profiles_text(rows, bot_username: str = "", total: tuple[int, int] = (0, 0),
                          rf_total: tuple[int, int] | None = None, outside: int = 0) -> str:
    """«📊 Трафик за 09.26: <итог>», под шапкой — РФ-итог и «🧐 Вне профилей»
    (только при ненулевых значениях), затем профили по убыванию общего трафика
    (нулевые не выводятся) со своей РФ-строкой."""
    head = [ui.head(f"📊 Трафик за {month_label()}", value=_total(*total))]
    if rf_total and int(rf_total[0]) + int(rf_total[1]) > 0:
        head.append(sub_line(f"🇷🇺 {ROUTING_NAME} (все): {rf_value(*rf_total)}"))
        # «Вне профилей» — часть РФ-итога (удалённые устройства и первые минуты
        # новых), поэтому под шапкой, а не записью среди профилей
        if int(outside or 0) >= _BYTES_PER_GB // 100:
            head.append(sub_line(_OUTSIDE.format(v=human_bytes(outside))))
    branches = []
    live = [(c, rx, tx, rf) for c, rx, tx, rf in rows if int(rx) + int(tx) > 0]
    for c, rx, tx, rf in live[:_LIST_CAP]:
        branches.append((f"👤 {_deep_link(bot_username, f'{TRAFFIC_PAYLOAD}-{c.id}', c.name)}: "
                         f"{_total(rx, tx)}", _rf_sub(rf)))
    if len(live) > _LIST_CAP:
        branches.append((f"… и ещё {len(live) - _LIST_CAP}", []))
    return "\n".join(head) + "\n\n" + (tree(branches) if branches else "Трафика за месяц ещё нет")


def traffic_devices_text(client, rows, total: tuple[int, int] = (0, 0),
                         rf_total: tuple[int, int] | None = None, bot_username: str = "") -> str:
    """«📊 Трафик за 09.26: <итог> · профиль [Ксюша]» — то же по устройствам профиля."""
    head = [ui.head(f"📊 Трафик за {month_label()}", value=_total(*total),
                    meta=[f"профиль {profile_link(client, bot_username)}"])]
    if rf_total and int(rf_total[0]) + int(rf_total[1]) > 0:
        head.append(sub_line(f"🇷🇺 {ROUTING_NAME} (все): {rf_value(*rf_total)}"))
    branches = []
    live = [r for r in sorted(rows, key=lambda r: -(int(r[1]) + int(r[2]))) if int(r[1]) + int(r[2]) > 0]
    for d, rx, tx, rf in live[:_LIST_CAP]:
        branches.append((f"{device_state(d, for_admin=True)} {_e(d.name)}: {_total(rx, tx)}",
                         _rf_sub(rf)))
    if len(live) > _LIST_CAP:
        branches.append((f"… и ещё {len(live) - _LIST_CAP}", []))
    return "\n".join(head) + "\n\n" + (tree(branches) if branches else "Трафика за месяц ещё нет")
