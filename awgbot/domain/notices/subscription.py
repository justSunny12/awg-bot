"""subscription.py — часть пакета notices (см. __init__): строка о днях паузы
в уведомлении о продлении подписки (её же показывает экран продления)."""

from __future__ import annotations

from awgbot.core import settings
from awgbot.util.textfmt import _days


def _pause_kind_ru(kind: str) -> str:
    return "годовой" if kind == "year" else "месячной"


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
        return f"⏸️ Дни паузы не добавлены: у {_pause_kind_ru(pc.kind)} подписки их максимум {pc.cap}"
    full = (settings.get_int("pause.pause_max_total_days", 28) if pc.kind == "year"
            else settings.get_int("pause.monthly_pause_days", 2))
    partial = pc.added < full
    note = ("" if not partial
            else " (максимум)" if pc.kind == "year"
            else " (максимум для месячной подписки)")
    return f"⏸️ Дней паузы +{pc.added} → {pc.after}{note}"
