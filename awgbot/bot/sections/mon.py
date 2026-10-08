"""«🩺 Мониторинг»: опрос, замеров до алерта, порог простоя роли (у агента —
молчание линка, хранится в секундах, показывается в минутах вверх), звук 24/7."""
from __future__ import annotations

from aiogram.utils.keyboard import InlineKeyboardBuilder

from awgbot.bot import texts
from awgbot.core import settings

from ._kb import _chk, back_button

ID, LABEL, BACK = "mon", "🩺 Мониторинг", "root"
KEYS = ("app.scheduler.monitor_minutes", "app.monitoring.alert_streak",
        "app.monitoring.service_failure_alert_minutes", "app.monitoring.service_failure_alert_loud",
        "app.gateway.monitor_minutes", "app.gateway.handshake_max_age", "app.gateway.link_alert_loud")
BOUNDS = {
    "app.scheduler.monitor_minutes": (1, 1440, "Частота опроса", "мин"),
    "app.monitoring.alert_streak": (1, 100, "Замеров до алерта", ""),
    "app.monitoring.service_failure_alert_minutes": (1, 1440, "Порог простоя", "мин"),
    "app.gateway.monitor_minutes": (1, 1440, "Частота опроса", "мин"),
    "app.gateway.handshake_max_age": (1, 1440, "Линк молчит дольше", "мин"),   # хранится в секундах
}
CYCLES: dict = {}
ACTIONS: dict = {}


def outage_minutes(br) -> int:
    """Порог простоя в минутах — вверх: 90 с показываем как 2 мин."""
    raw = settings.get_int(br.keys.outage, 5 * br.keys.outage_scale)
    return max(1, -(-raw // br.keys.outage_scale))


def text(br) -> str:
    return texts.settings_mon_text(br)


def keyboard(br):
    s = settings
    kb = InlineKeyboardBuilder()
    kb.button(text=f"⏱ Опрос: {s.get_int(br.keys.monitor_minutes, 3)} мин",
              callback_data=br.cb.pack(ID, "edit", br.keys.monitor_minutes))
    kb.button(text=f"🔢 Замеров: {s.get_int('app.monitoring.alert_streak', 5)}",
              callback_data=br.cb.pack(ID, "edit", "app.monitoring.alert_streak"))
    kb.button(text=f"{br.mon_outage_button}: {outage_minutes(br)} мин",
              callback_data=br.cb.pack(ID, "edit", br.keys.outage))
    loud = s.get_bool(br.keys.outage_loud, True)
    kb.button(text=f"{_chk(loud)} Звук 24/7", callback_data=br.cb.pack(ID, "toggle", br.keys.outage_loud))
    kb.adjust(2, 2)
    kb.row(back_button(br))
    return kb.as_markup()


async def screen(br, services, key: str = ""):
    return text(br), keyboard(br)
