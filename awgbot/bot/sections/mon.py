"""«🩺 Мониторинг»: опрос, замеров до алерта, порог простоя роли (у агента —
молчание линка, хранится в секундах, показывается в минутах вверх), звук 24/7."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from awgbot.bot import texts
from awgbot.bot import ui
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
DEFAULTS = {
    "app.scheduler.monitor_minutes": 3, "app.gateway.monitor_minutes": 3,
    "app.monitoring.alert_streak": 5,
    "app.monitoring.service_failure_alert_minutes": 5, "app.gateway.handshake_max_age": 300,
}
SCALE = {"app.gateway.handshake_max_age": 60}      # ввод и экран — минуты, conf — секунды
CYCLES: dict = {}
ACTIONS: dict = {}


def outage_minutes(br) -> int:
    """Порог простоя роли в минутах — вверх: 90 с показываем как 2 мин."""
    k = SCALE.get(br.keys.outage, 1)
    raw = settings.get_int(br.keys.outage, DEFAULTS[br.keys.outage])
    return max(1, -(-raw // k))


def text(br) -> str:
    return texts.settings_mon_text(br)


def keyboard(br) -> InlineKeyboardMarkup:
    s = settings
    streak = s.get_int("app.monitoring.alert_streak", DEFAULTS["app.monitoring.alert_streak"])
    loud = s.get_bool(br.keys.outage_loud, True)
    return ui.rows(
        [(f"⏱ Опрос: {s.get_int(br.keys.monitor_minutes, DEFAULTS[br.keys.monitor_minutes])} мин",
          br.cb.pack(ID, "edit", br.keys.monitor_minutes)),
         (f"🔢 Замеров: {streak}", br.cb.pack(ID, "edit", "app.monitoring.alert_streak"))],
        [(f"{br.mon_outage_button}: {outage_minutes(br)} мин", br.cb.pack(ID, "edit", br.keys.outage)),
         (f"{_chk(loud)} Звук 24/7", br.cb.pack(ID, "toggle", br.keys.outage_loud))],
        back_button(br))


async def screen(br, services, key: str = ""):
    return text(br), keyboard(br)
