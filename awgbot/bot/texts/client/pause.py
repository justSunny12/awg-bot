"""pause.py — часть пакета client (см. __init__): пауза."""

from __future__ import annotations

from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.bot import ui
from ..fmt import _e, _days_word
from .common import TELEGRAM_RISK


# ─────────────────────────────────────────────────────────────────────────────
# Пауза
# ─────────────────────────────────────────────────────────────────────────────

PAUSE_WARNING_LINE = f"⚠️ На паузе VPN выключен. {TELEGRAM_RISK}, снять паузу будет нечем"


def pause_ask(available_days: int, *, email_resume: bool = False) -> str:
    """Экран паузы: сколько доступно, что происходит, предупреждение — только
    когда аварийного выхода по почте нет."""
    lines = [ui.head("⏸️ Пауза", meta=[f"до {available_days} дн."]),
             "Действие подписки приостановится; снимешь раньше — неизрасходованные дни вернутся"]
    if not email_resume:
        lines.append(PAUSE_WARNING_LINE)
    return "\n".join(lines)


def pause_other_prompt(available_days: int) -> str:
    return f"✏️ Дней, 1–{available_days}"


def pause_days_bad(available_days: int) -> str:
    return f"⚠️ Нужно целое число от 1 до {available_days}"


def pause_emergency_code(code: str, address: str) -> str:
    return (f"🆘 Если потеряешь доступ к боту — отправь письмо на <code>{_e(address)}</code> "
            f"с темой <code>{_e(code)}</code>: пауза снимется сама. Код одноразовый. "
            "Сохрани адрес и код в заметках")


def pause_entered_summary(until: str) -> str:
    """Итог входа в паузу — остаётся в чате. until — дата авто-возобновления
    (уже в формате экрана)."""
    return f"⏸️ Подписка на паузе до {until} — снять раньше можно в разделе «💳 Подписка»"


def pause_unavailable() -> str:
    md = settings.get_int("pause.monthly_pause_days", 2)
    return ("Пауза сейчас недоступна: на счету нет дней. Годовая подписка даёт "
            f"{settings.get_int('pause.pause_max_total_days', 28)} дней за период, "
            f"ежемесячная — по {md} {_days_word(md)} за каждое своевременное продление")


def pause_limit_exhausted() -> str:
    return "Дни паузы на счету закончились — пополнится при продлении подписки"


def pause_resumed_self(actual_days: int, new_end) -> str:
    return (f"▶️ Пауза снята · {actual_days} дн. израсходовано · "
            f"подписка до {timeutil.fmt_end_ui(new_end)}")
