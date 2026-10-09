"""«🔔 Уведомления»: тихие часы и их границы, алерты хоста и пороги (у роли с
порогом температуры — четыре кнопки по две в ряд), аварии на e-mail; у роли с
подразделом «👥 События» — кнопка в него."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from awgbot.bot import texts
from awgbot.bot import ui
from awgbot.core import settings

from ._kb import _chk, back_button

ID, LABEL, BACK = "notify", "🔔 Уведомления", "root"
KEYS = ("quiet_hours.quiet_hours_enabled", "quiet_hours.quiet_hours_start", "quiet_hours.quiet_hours_end",
        "resource_alerts.enabled", "resource_alerts.thresholds_percent.cpu",
        "resource_alerts.thresholds_percent.ram", "resource_alerts.thresholds_percent.disk",
        "notifications.email_fallback", "app.gateway.temp_alert_c")
BOUNDS = {
    "quiet_hours.quiet_hours_start": (0, 23, "Тихие часы с", "ч"),
    "quiet_hours.quiet_hours_end": (0, 23, "Тихие часы до", "ч"),
    "resource_alerts.thresholds_percent.cpu": (1, 100, "Порог CPU", "%"),
    "resource_alerts.thresholds_percent.ram": (1, 100, "Порог RAM", "%"),
    "resource_alerts.thresholds_percent.disk": (1, 100, "Порог диска", "%"),
    "app.gateway.temp_alert_c": (40, 100, "Порог температуры", "°C"),
}
DEFAULTS = {
    "quiet_hours.quiet_hours_enabled": True, "quiet_hours.quiet_hours_start": 20, "quiet_hours.quiet_hours_end": 7,
    "resource_alerts.enabled": True, "resource_alerts.thresholds_percent.cpu": 80,
    "resource_alerts.thresholds_percent.ram": 80, "resource_alerts.thresholds_percent.disk": 80,
    "notifications.email_fallback": False, "app.gateway.temp_alert_c": 75,
}
CYCLES: dict = {}
ACTIONS: dict = {}


def _int(key: str) -> int:
    return settings.get_int(key, DEFAULTS[key])


def _bool(key: str) -> bool:
    return settings.get_bool(key, DEFAULTS[key])


def text(br) -> str:
    return texts.settings_notify_text(br)


def keyboard(br) -> InlineKeyboardMarkup:
    from . import available
    qh = _bool("quiet_hours.quiet_hours_enabled")
    ra = _bool("resource_alerts.enabled")
    cpu = (f"CPU {_int('resource_alerts.thresholds_percent.cpu')}%", br.cb.pack(ID, "edit", "resource_alerts.thresholds_percent.cpu"))
    ram = (f"RAM {_int('resource_alerts.thresholds_percent.ram')}%", br.cb.pack(ID, "edit", "resource_alerts.thresholds_percent.ram"))
    disk = (f"Диск {_int('resource_alerts.thresholds_percent.disk')}%", br.cb.pack(ID, "edit", "resource_alerts.thresholds_percent.disk"))
    temp = (f"{_int(br.keys.temp_alert)} °C", br.cb.pack(ID, "edit", br.keys.temp_alert)) if br.keys.temp_alert else None
    mail = (f"{_chk(_bool('notifications.email_fallback'))} Аварии на e-mail",
            br.cb.pack(ID, "toggle", "notifications.email_fallback"))
    events = available(br, "ncl")           # подраздел событий — рядом с тумблером, «Назад» своим рядом
    return ui.rows(
        (f"{_chk(qh)} Тихие часы", br.cb.pack(ID, "toggle", "quiet_hours.quiet_hours_enabled")),
        [(f"С {_int('quiet_hours.quiet_hours_start'):02d}:00", br.cb.pack(ID, "edit", "quiet_hours.quiet_hours_start")),
         (f"До {_int('quiet_hours.quiet_hours_end'):02d}:00", br.cb.pack(ID, "edit", "quiet_hours.quiet_hours_end"))] if qh else None,
        (f"{_chk(ra)} Алерты хоста", br.cb.pack(ID, "toggle", "resource_alerts.enabled")),
        *(([cpu, ram], [disk, temp]) if temp else ([cpu, ram, disk],)) if ra else (),
        [mail, ("👥 События", br.cb.pack("ncl")) if events else back_button(br)],
        back_button(br) if events else None)


async def screen(br, services, key: str = ""):
    return text(br), keyboard(br)
