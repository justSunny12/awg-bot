"""«👥 События» — о каких событиях профилей сообщать админу; вложенный раздел
«🔔 Уведомлений», есть у роли, в чьём словаре он назван подразделом."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from awgbot.bot import texts
from awgbot.bot import ui
from awgbot.core import settings

from ._kb import _tick, back_button

ID, LABEL, BACK = "ncl", "👥 События", "notify"
CLIENT_EVENT_LABELS = (("activation", "Активация"), ("grace", "Отсрочка"),
                       ("over_limit", "Лимит исчерпан"), ("bonus", "Бонусный объём"))
KEYS = tuple(f"notifications.client_events.{k}" for k, _ in CLIENT_EVENT_LABELS)
BOUNDS: dict = {}
CYCLES: dict = {}
ACTIONS: dict = {}


def text(br) -> str:
    return texts.SETTINGS_NOTIFY_CLIENTS


def keyboard(br) -> InlineKeyboardMarkup:
    return ui.rows(
        *ui.grid([(f"{_tick(settings.get_bool(f'notifications.client_events.{key}', True))} {lbl}",
                   br.cb.pack(ID, "toggle", f"notifications.client_events.{key}"))
                  for key, lbl in CLIENT_EVENT_LABELS], 2),
        back_button(br, BACK))


async def screen(br, services, key: str = ""):
    return text(br), keyboard(br)
