"""subscription.py — часть пакета client (см. __init__): экран «💳 Подписка»."""

from __future__ import annotations

from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.bot import ui
from awgbot.core.enums import SubStatus
from ..fmt import details, access_status_line, gb_str, _n_devices
from ..routing import ROUTING_NAME
from .common import _pause_visibility, _pause_until, ADMIN_PAUSE_STATUS
# строка о днях паузы — уведомление домена о продлении, текст общий
from awgbot.domain.notices.subscription import _pause_kind_ru, pause_credit_line  # noqa: F401


# ─────────────────────────────────────────────────────────────────────────────
# Карточка пользователя (админ)
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Подписка
# ─────────────────────────────────────────────────────────────────────────────


_KIND_LABELS = {"day": "на день", "week": "на неделю", "month": "ежемесячная",
                "year": "годовая", "never": "бессрочная"}


def subscription_kind_label(kind) -> str:
    return _KIND_LABELS.get(str(kind or ""), str(kind or "—"))


def pause_rules_details() -> str:
    """Свёрнутое «подробнее» о том, как копятся дни паузы."""
    year_days = settings.get_int("pause.pause_max_total_days", 28)
    month_days = settings.get_int("pause.monthly_pause_days", 2)
    return details("Дни паузы копятся за продление:\n"
                   f"на месяц вовремя — +{month_days} дн. (до {12 * month_days});\n"
                   f"на год — +{year_days} дн. (до {2 * year_days})")


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


EXPIRED_HINT = "Продлить — у администратора"


def _expired(client) -> bool:
    """Истекла по статусу или по сроку: статус переключает такт, а срок уже вышел."""
    return client.status != SubStatus.ACTIVE or bool(
        client.period_end and timeutil.remaining_seconds(timeutil.parse_iso(client.period_end)) <= 0)


def subscription_text(client, *, routing_visible: bool) -> str:
    """Экран «💳 Подписка»: строка состояния (только когда доступа нет), тип и
    статус, период и остаток, после пустой строки — пауза (счёт или текущая;
    на истёкшей вместо неё — что делать), лимиты. «🇷🇺 РФ-доступ» в лимитах —
    только когда выдан."""
    paused, mode, pause_visible = _pause_visibility(client)
    kind = subscription_kind_label(client.period_kind)
    access = access_status_line(client)
    lines = [access] if access and (pause_visible or not paused) else []
    if pause_visible and mode == "user":
        until = _pause_until(client)
        lines.append(ui.head("💳 Подписка", value=kind,
                             meta=[f"⏸️ на паузе до {until}" if until else "⏸️ на паузе"]))
    elif pause_visible:
        lines.append(ui.head("💳 Подписка", value=kind, meta=[ADMIN_PAUSE_STATUS]))
    else:
        lines.append(ui.head("💳 Подписка", value=kind, meta=[subscription_status_only(client, expiring=True)]))
    start = timeutil.parse_iso(client.period_start) if client.period_start else None
    end_iso = client.effective_period_end
    expired = _expired(client)
    if end_iso:
        end = timeutil.parse_iso(end_iso)
        period = "📅 " + (timeutil.fmt_period_ui(start, end) if start else f"до {timeutil.fmt_end_ui(end)}")
        if not pause_visible and client.status == SubStatus.ACTIVE:
            period += f" · ост. {timeutil.remaining_brief(end)}"
        lines.append(period)
        if expired:
            lines.append(EXPIRED_HINT)            # взять паузу нельзя — блок паузы не выводим
        elif pause_visible and mode == "user":
            since = timeutil.parse_iso(client.pause_active_since) if client.pause_active_since else None
            reserved = int(client.pause_reserved_days)
            used = timeutil.ceil_days(timeutil.elapsed(since)) if since else 0
            used = max(0, min(used, reserved))
            until = _pause_until(client)
            lines.append("")
            lines.append(f"⏸️ Пауза с {timeutil.fmt_date_ui(since) if since else '—'} по {until} · "
                         f"прошло {used} из {reserved} дн.")
            lines.append("Снимешь раньше — неизрасходованные дни вернутся на счёт")
        elif not pause_visible:
            lines.append("")
            lines.append(pause_balance_line(client))
            if str(client.period_kind or "") in ("year", "month"):
                lines.append(pause_rules_details())
    lines += ["", _limits_line(client, routing_visible)]
    return "\n".join(l for l in lines if l is not None)


def subscription_status_only(client, *, expiring: bool = False) -> str:
    """Только статус подписки: «🟢 активна», «🟡 истекает 12.10 18:00»,
    «🔴 истекла», «⏸️ на паузе» / «⏸️ на паузе (поставил администратор)»."""
    _, mode, pause_visible = _pause_visibility(client)
    if pause_visible:
        return "⏸️ на паузе" if mode == "user" else ADMIN_PAUSE_STATUS
    if _expired(client):
        return "🔴 истекла"
    if expiring and client.period_end and client.notified_thresholds:
        end = timeutil.parse_iso(client.period_end)
        return f"🟡 истекает {timeutil.fmt_end_ui(end)}"
    return "🟢 активна"


__all__ = [
    "_pause_kind_ru", "pause_credit_line", "_KIND_LABELS", "subscription_kind_label",
    "pause_rules_details", "pause_balance_line", "_limits_line", "subscription_text",
    "subscription_status_only", "EXPIRED_HINT", "_expired"]
