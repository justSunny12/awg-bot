"""«🔔 Уведомления»: тихие часы и их границы, алерты хоста и пороги (у роли с
порогом температуры — четыре кнопки по две в ряд), аварии на e-mail; у роли с
подразделом «👥 События» — кнопка в него."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from awgbot.bot import texts
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
    kb = InlineKeyboardBuilder()
    rows: list[int] = []
    qh = _bool("quiet_hours.quiet_hours_enabled")
    kb.button(text=f"{_chk(qh)} Тихие часы", callback_data=br.cb.pack(ID, "toggle", "quiet_hours.quiet_hours_enabled"))
    rows.append(1)
    if qh:
        kb.button(text=f"С {_int('quiet_hours.quiet_hours_start'):02d}:00",
                  callback_data=br.cb.pack(ID, "edit", "quiet_hours.quiet_hours_start"))
        kb.button(text=f"До {_int('quiet_hours.quiet_hours_end'):02d}:00",
                  callback_data=br.cb.pack(ID, "edit", "quiet_hours.quiet_hours_end"))
        rows.append(2)
    ra = _bool("resource_alerts.enabled")
    kb.button(text=f"{_chk(ra)} Алерты хоста", callback_data=br.cb.pack(ID, "toggle", "resource_alerts.enabled"))
    rows.append(1)
    if ra:
        kb.button(text=f"CPU {_int('resource_alerts.thresholds_percent.cpu')}%",
                  callback_data=br.cb.pack(ID, "edit", "resource_alerts.thresholds_percent.cpu"))
        kb.button(text=f"RAM {_int('resource_alerts.thresholds_percent.ram')}%",
                  callback_data=br.cb.pack(ID, "edit", "resource_alerts.thresholds_percent.ram"))
        kb.button(text=f"Диск {_int('resource_alerts.thresholds_percent.disk')}%",
                  callback_data=br.cb.pack(ID, "edit", "resource_alerts.thresholds_percent.disk"))
        if br.keys.temp_alert:
            kb.button(text=f"{_int(br.keys.temp_alert)} °C",
                      callback_data=br.cb.pack(ID, "edit", br.keys.temp_alert))
            rows += [2, 2]
        else:
            rows.append(3)
    ef = _bool("notifications.email_fallback")
    kb.button(text=f"{_chk(ef)} Аварии на e-mail", callback_data=br.cb.pack(ID, "toggle", "notifications.email_fallback"))
    if available(br, "ncl"):
        # подраздел событий — рядом с тумблером, «Назад» своим рядом
        kb.button(text="👥 События", callback_data=br.cb.pack("ncl"))
        rows.append(2)
        kb.adjust(*rows)
        kb.row(back_button(br))
    else:
        kb.add(back_button(br))
        rows.append(2)
        kb.adjust(*rows)
    return kb.as_markup()


async def screen(br, services, key: str = ""):
    return text(br), keyboard(br)
