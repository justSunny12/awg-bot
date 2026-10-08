"""«👥 События» — о каких событиях профилей сообщать админу; вложенный раздел
«🔔 Уведомлений», есть у роли, в чьём словаре он назван подразделом."""
from __future__ import annotations

from aiogram.utils.keyboard import InlineKeyboardBuilder

from awgbot.bot import texts
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


def keyboard(br):
    kb = InlineKeyboardBuilder()
    for key, lbl in CLIENT_EVENT_LABELS:
        on = settings.get_bool(f"notifications.client_events.{key}", True)
        kb.button(text=f"{_tick(on)} {lbl}",
                  callback_data=br.cb.pack(ID, "toggle", f"notifications.client_events.{key}"))
    kb.adjust(2)
    kb.row(back_button(br, BACK))
    return kb.as_markup()


async def screen(br, services, key: str = ""):
    return text(br), keyboard(br)
