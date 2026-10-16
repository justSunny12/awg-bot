"""root.py — часть пакета settings (см. __init__): корень, уведомления, подписки, мониторинг."""

from __future__ import annotations

from awgbot.bot import ui
from ..fmt import _e, plural_ru, details
from ..updates import _ver


# ── корень ───────────────────────────────────────────────────────────────────

def settings_root_text(installed: str | None = None) -> str:
    """«⚙️ Настройки · v3.1.0» — заголовок и версия, больше ничего: разделы
    говорят за себя кнопками."""
    from awgbot.core import config
    return ui.head("⚙️ Настройки", meta=[_e(_ver(installed if installed is not None else config.INSTALLED_VERSION))])


# ── 🔔 Уведомления ───────────────────────────────────────────────────────────

SETTINGS_NOTIFY_CLIENTS = ui.head("👥 События профилей") + " — о чём сообщать"


def settings_notify_text(br) -> str:
    """Тихие часы и алерты хоста — строками по текущим значениям; про аварии
    на e-mail — под «подробнее». Порог температуры и перечень аварий — роли."""
    from awgbot.bot.sections.notify import _bool, _int
    lines = [ui.head("🔔 Уведомления")]
    if _bool("quiet_hours.quiet_hours_enabled"):
        lines.append(f"Тихие часы {_int('quiet_hours.quiet_hours_start'):02d}:00–"
                     f"{_int('quiet_hours.quiet_hours_end'):02d}:00 МСК — без звука, кроме аварий")
    else:
        lines.append("Тихие часы выключены — уведомления со звуком круглые сутки")
    if _bool("resource_alerts.enabled"):
        line = (f"Алерты хоста: CPU {_int('resource_alerts.thresholds_percent.cpu')}% · "
                f"RAM {_int('resource_alerts.thresholds_percent.ram')}% · "
                f"диск {_int('resource_alerts.thresholds_percent.disk')}%")
        if br.keys.temp_alert:
            line += f" · {_int(br.keys.temp_alert)} °C"
        lines.append(line)
    else:
        lines.append("Алерты хоста выключены")
    lines.append(details(f"Аварии на e-mail — только когда Telegram недоступен: {br.mail_alarms}"))
    return "\n".join(lines)


# ── 💳 Подписки ──────────────────────────────────────────────────────────────

def settings_subs_text() -> str:
    from awgbot.core import settings as s
    from awgbot.domain.services.traffic import bonus as _bonus
    spec = _bonus.current(s)
    grace = s.get_int("grace.grace_days", 14)
    year = s.get_int("pause.pause_max_total_days", 28)
    month = s.get_int("pause.monthly_pause_days", 2)
    return "\n".join([
        ui.head("💳 Подписки", meta=["правила для всех"]),
        f"Бонус при исчерпании: {_bonus.screen_text(spec)}",
        f"Отсрочка: {grace} дн.",
        f"Пауза: годовым +{year} дн. (копится до {2 * year}), месячным +{month} дн. (до {12 * month})",
        details("Действуют с момента правки и на уже выданные подписки тоже.\n"
                "• Бонус — сколько трафика добавляется профилю разово, когда он упёрся в лимит; "
                "0 — без бонуса, доступ приостанавливается сразу.\n"
                "• Отсрочка — сколько дней профиль ещё работает после окончания подписки, чтобы "
                "подписка, истёкшая в отпуске или ночью, не обрывала доступ без предупреждения.\n"
                "• Пауза останавливает срок подписки; неиспользованные дни возвращаются в конце:\n"
                "годовой — за каждое продление, копятся за два продления;\n"
                "месячной — за продление вовремя, копятся за 12;\n"
                "подписка истекла или ушла в отсрочку — за следующее месячное продление дни паузы не начисляются.\n"
                "Лимиты конкретного профиля — в его карточке"),
    ])


# ── 🩺 Мониторинг ────────────────────────────────────────────────────────────

def settings_mon_text(br) -> str:
    """Одной строкой: опрос, порог алерта, порог простоя роли (у агента —
    молчание линка; хранится в секундах, показывается в минутах вверх)."""
    from awgbot.core import settings as s
    from awgbot.bot.sections import mon
    loud = s.get_bool(br.keys.outage_loud, True)
    streak = s.get_int("app.monitoring.alert_streak", mon.DEFAULTS["app.monitoring.alert_streak"])
    mins = mon.outage_minutes(br)
    return ui.head("🩺 Мониторинг", meta=[
        f"опрос раз в {s.get_int(br.keys.monitor_minutes, mon.DEFAULTS[br.keys.monitor_minutes])} мин",
        f"алерт после {streak} {plural_ru(streak, 'плохого замера', 'плохих замеров', 'плохих замеров')}",
        f"{br.mon_outage} {mins} мин — " + ("со звуком круглые сутки" if loud else "по правилам тихих часов")])
