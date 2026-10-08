"""«🔔 Уведомления»: тихие часы и их границы, алерты хоста и пороги (у роли с
порогом температуры — четыре кнопки по две в ряд), аварии на e-mail; у роли с
подразделом «👥 События» — кнопка в него."""
from __future__ import annotations

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
CYCLES: dict = {}
ACTIONS: dict = {}


def text(br) -> str:
    return texts.settings_notify_text(br)


def keyboard(br):
    from . import available
    s = settings
    kb = InlineKeyboardBuilder()
    rows: list[int] = []
    qh = s.get_bool("quiet_hours.quiet_hours_enabled", True)
    kb.button(text=f"{_chk(qh)} Тихие часы", callback_data=br.cb.pack(ID, "toggle", "quiet_hours.quiet_hours_enabled"))
    rows.append(1)
    if qh:
        kb.button(text=f"С {s.get_int('quiet_hours.quiet_hours_start', 20):02d}:00",
                  callback_data=br.cb.pack(ID, "edit", "quiet_hours.quiet_hours_start"))
        kb.button(text=f"До {s.get_int('quiet_hours.quiet_hours_end', 7):02d}:00",
                  callback_data=br.cb.pack(ID, "edit", "quiet_hours.quiet_hours_end"))
        rows.append(2)
    ra = s.get_bool("resource_alerts.enabled", True)
    kb.button(text=f"{_chk(ra)} Алерты хоста", callback_data=br.cb.pack(ID, "toggle", "resource_alerts.enabled"))
    rows.append(1)
    if ra:
        kb.button(text=f"CPU {s.get_int('resource_alerts.thresholds_percent.cpu', 80)}%",
                  callback_data=br.cb.pack(ID, "edit", "resource_alerts.thresholds_percent.cpu"))
        kb.button(text=f"RAM {s.get_int('resource_alerts.thresholds_percent.ram', 80)}%",
                  callback_data=br.cb.pack(ID, "edit", "resource_alerts.thresholds_percent.ram"))
        kb.button(text=f"Диск {s.get_int('resource_alerts.thresholds_percent.disk', 80)}%",
                  callback_data=br.cb.pack(ID, "edit", "resource_alerts.thresholds_percent.disk"))
        if br.keys.temp_alert:
            kb.button(text=f"{s.get_int(br.keys.temp_alert, 75)} °C",
                      callback_data=br.cb.pack(ID, "edit", br.keys.temp_alert))
            rows += [2, 2]
        else:
            rows.append(3)
    ef = s.get_bool("notifications.email_fallback", False)
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
