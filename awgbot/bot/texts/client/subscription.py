"""subscription.py — часть пакета client (см. __init__): экран «💳 Подписка»."""

from __future__ import annotations

from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.bot import ui
from awgbot.core.enums import SubStatus
from ..fmt import details, access_status_line, gb_str, _n_devices, _days
from ..routing import ROUTING_NAME
from .common import _pause_visibility, _pause_until


# ─────────────────────────────────────────────────────────────────────────────
# Карточка клиента (админ)
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Подписка
# ─────────────────────────────────────────────────────────────────────────────


_KIND_LABELS = {"day": "на день", "week": "на неделю", "month": "ежемесячная",
                "year": "годовая", "never": "бессрочная"}


def subscription_kind_label(kind) -> str:
    return _KIND_LABELS.get(str(kind or ""), str(kind or "—"))


def _pause_kind_ru(kind: str) -> str:
    return "годовой" if kind == "year" else "ежемесячной"


def pause_credit_line(pc) -> str:
    if pc is None or pc.kind not in ("year", "month"):
        return ""
    if pc.reason == "expired":
        return ("⏸️ Дни паузы за этот период не начислены: подписка продлена после истечения. "
                f"Доступно {_days(pc.after)}")
    if pc.reason == "grace":
        return ("⏸️ Дни паузы за этот период не начислены: в прошлом периоде использована "
                f"отсрочка. Доступно {_days(pc.after)}")
    if pc.reason == "cap":
        return ("⏸️ Дни паузы не добавлены: достигнуто максимальное количество для "
                f"{_pause_kind_ru(pc.kind)} подписки ({pc.cap})")
    full = (settings.get_int("pause.pause_max_total_days", 28) if pc.kind == "year"
            else settings.get_int("pause.monthly_pause_days", 2))
    partial = pc.added < full
    note = ("" if not partial
            else " (максимум)" if pc.kind == "year"
            else " (максимум для ежемесячной подписки)")
    return f"⏸️ Дней паузы +{pc.added} → {pc.after}{note}"


def pause_rules_details() -> str:
    """Свёрнутое «подробнее» о том, как копятся дни паузы."""
    year_days = settings.get_int("pause.pause_max_total_days", 28)
    month_days = settings.get_int("pause.monthly_pause_days", 2)
    return details(f"+{month_days} дн. паузы за своевременное продление на месяц "
                   f"(до {12 * month_days} дн.),\n+{year_days} дн. за продление на год (до {2 * year_days} дн.)")


def pause_balance_line(client) -> str:
    """«⏸️ Пауза: 14 дн. доступно» — всем, кроме бессрочных."""
    if not client.effective_period_end:
        return ""
    bal = int(client.pause_balance_days)
    return f"⏸️ Пауза: {bal} дн. доступно" if bal else "⏸️ Пауза: 0 дн. доступно"


def _limits_line(client, routing_visible: bool) -> str:
    parts = [f"{gb_str(client.traffic_limit) if client.traffic_limit else '∞ ГБ'} в месяц",
             _n_devices(client.device_limit) if client.device_limit else "∞ устройств"]
    if routing_visible:
        parts.append(f"🇷🇺 {ROUTING_NAME}")
    return "Включено в подписку: " + " · ".join(parts)


def subscription_text(client, *, routing_visible: bool) -> str:
    """Экран «💳 Подписка»: тип и статус, период и остаток, после пустой строки —
    пауза (счёт или текущая), лимиты. «🇷🇺 РФ-доступ» в лимитах — только когда выдан."""
    paused, mode, pause_visible = _pause_visibility(client)
    kind = subscription_kind_label(client.period_kind)
    access = access_status_line(client)
    lines = [access] if access and (pause_visible or not paused) else []
    if pause_visible and mode == "user":
        lines.append(ui.head("💳 Подписка", value=kind))          # «на паузе» — строкой ниже, без повтора
    elif pause_visible:
        lines.append(ui.head("💳 Подписка", value=kind, meta=["⏸️ приостановлена администратором"]))
    else:
        lines.append(ui.head("💳 Подписка", value=kind, meta=[subscription_status_only(client, expiring=True)]))
    start = timeutil.parse_iso(client.period_start) if client.period_start else None
    end_iso = client.effective_period_end
    if end_iso:
        end = timeutil.parse_iso(end_iso)
        period = "📅 " + (timeutil.fmt_period_ui(start, end) if start else f"до {timeutil.fmt_end_ui(end)}")
        if not pause_visible and client.status == SubStatus.ACTIVE:
            period += f" · ост. {timeutil.remaining_brief(end)}"
        lines.append(period)
        if pause_visible and mode == "user":
            since = timeutil.parse_iso(client.pause_active_since) if client.pause_active_since else None
            reserved = int(client.pause_reserved_days)
            used = timeutil.ceil_days((timeutil.now() - since).total_seconds()) if since else 0
            used = max(0, min(used, reserved))
            until = _pause_until(client)
            lines.append("")
            lines.append(f"⏸️ на паузе с {timeutil.fmt_date_ui(since) if since else '—'}, до {until} · "
                         f"израсходовано {used} из {reserved} дн. — неиспользованный остаток вернётся при досрочном возобновлении")
        elif not pause_visible:
            lines.append("")
            lines.append(pause_balance_line(client))
            if str(client.period_kind or "") in ("year", "month"):
                lines.append(pause_rules_details())
    lines += ["", _limits_line(client, routing_visible)]
    return "\n".join(l for l in lines if l is not None)


def subscription_status_only(client, *, expiring: bool = False) -> str:
    """Только статус подписки: «🟢 активна», «🟡 истекает 12.10 18:00»,
    «🔴 истекла», «⏸️ на паузе» / «⏸️ приостановлена администратором»."""
    _, mode, pause_visible = _pause_visibility(client)
    if pause_visible:
        return "⏸️ на паузе" if mode == "user" else "⏸️ приостановлена администратором"
    if client.status != SubStatus.ACTIVE or (
            client.period_end and timeutil.remaining_seconds(timeutil.parse_iso(client.period_end)) <= 0):
        return "🔴 истекла"                      # и по сроку: статус переключает такт, а срок уже вышел
    if expiring and client.period_end and client.notified_thresholds:
        end = timeutil.parse_iso(client.period_end)
        return f"🟡 истекает {timeutil.fmt_end_ui(end)}"
    return "🟢 активна"
