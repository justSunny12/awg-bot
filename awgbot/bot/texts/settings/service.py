"""service.py — часть пакета settings (см. __init__): «🔧 Сервис» и расписание
проверки обновлений; раздел «⬆️ Обновления» — texts/fleet.py (набор)."""

from __future__ import annotations

from awgbot.bot import ui
from ..fmt import details


# ── 🔧 Сервис ────────────────────────────────────────────────────────────────

def svc_confirm_awg(br, carries: bool = True) -> str:
    """«🔁 Перезапустить AWG?» и цена словами роли; carries — роль сейчас
    несёт трафик (у агента предупреждение про РФ-доступ только тогда)."""
    return ui.confirm("🔁 Перезапустить AWG?", br.awg_restart_cost_carrying if carries else br.awg_restart_cost)


def svc_confirm_bot(br) -> str:
    return ui.confirm("🔁 Перезапустить бота?", br.bot_restart_cost)


def settings_svc_text(br, state: str = "", progress=None, available: bool = False,
                      carries: bool = True) -> str:
    """«🔧 Сервис»: цена перезапусков одной строкой роли — та же развилка, что
    в подтверждении (carries — роль несёт трафик); переезд — только у роли с
    has.migration и только когда идёт; часовой пояс — под «подробнее»,
    нераспознанный ключ timezone — открытой строкой ⚠️."""
    from ..admin import migration_counts
    from ..zone import svc_zone_line, svc_zone_warning
    lines = [ui.head("🔧 Сервис"), br.svc_about_carrying if carries else br.svc_about]
    if br.has.migration and state:
        p = progress
        nums = (f" переехали {migration_counts(p)}"
                if p is not None and getattr(p, "clients_total", 0) else "")
        lines.append(f"🚚 Переезд идёт:{nums} · выдаются только новые конфигурации")
    warn = svc_zone_warning(br)
    if warn:
        lines.append(warn)
    zone_line = svc_zone_line(br)
    if zone_line:
        lines.append(details(zone_line))
    return "\n".join(lines)


# ── ⬆️ Обновления ────────────────────────────────────────────────────────────

UPDATE_SCHEDULE_CYCLE = ("day", "week", "month")
UPDATE_SCHEDULE_LABELS = {"day": "день", "week": "неделя", "month": "месяц"}
