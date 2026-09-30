"""Клавиатуры разделов настроек, одинаковые у основного бота и агента шлюза:
уведомления, бэкапы и шифрование, почта, обновления, финишер порта SSH.
Роли различаются только колбэками — их даёт адаптер RoleCB; раскладка и
подписи одни, и разойтись им больше не с чего."""

from __future__ import annotations

from typing import Callable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from awgbot.core import settings
from .common import _chk

UPDATE_SCHEDULE_CYCLE = ("day", "week", "month")
UPDATE_SCHEDULE_LABELS = {"day": "день", "week": "неделя", "month": "месяц"}


def _day_label(day: int) -> str:
    """«1-е», «2-е», «3-е», «7-е» — число месяца с окончанием."""
    d = int(day)
    return f"{d}-е"


def backup_when_label(day: int, hour: int) -> str:
    return f"✏️ {_day_label(day)}, {int(hour):02d}:00"


class RoleCB:
    """Колбэки роли: toggle/edit/cycle по ключу настройки, do по имени
    действия раздела (в actions — packed-строки или CallbackData), back —
    кнопка «⬅️ Назад» раздела."""

    def __init__(self, *, toggle: Callable[[str], object], edit: Callable[[str], object],
                 cycle: Callable[[str], object], actions: dict, back: Callable[[str], object]):
        self._toggle, self._edit, self._cycle, self._actions, self._back = toggle, edit, cycle, actions, back

    def toggle(self, key: str):
        return self._toggle(key)

    def edit(self, key: str):
        return self._edit(key)

    def cycle_button(self, key: str, label: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=label, callback_data=_packed(self._cycle(key)))

    def do(self, name: str):
        return self._actions[name]

    def back_button(self, sec: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text="⬅️ Назад", callback_data=_packed(self._back(sec)))


def _packed(cb) -> str:
    return cb if isinstance(cb, str) else cb.pack()


def notify_rows(kb: InlineKeyboardBuilder, rc: RoleCB, *, temp: bool) -> list[int]:
    """Тихие часы и их границы, алерты хоста и пороги (у агента ещё порог
    температуры — четыре кнопки по две в ряд). Хвост раздела — у роли."""
    s = settings
    rows: list[int] = []
    qh = s.get_bool("quiet_hours.quiet_hours_enabled", True)
    kb.button(text=f"{_chk(qh)} Тихие часы", callback_data=rc.toggle("quiet_hours.quiet_hours_enabled"))
    rows.append(1)
    if qh:
        kb.button(text=f"С {s.get_int('quiet_hours.quiet_hours_start', 20):02d}:00",
                  callback_data=rc.edit("quiet_hours.quiet_hours_start"))
        kb.button(text=f"До {s.get_int('quiet_hours.quiet_hours_end', 7):02d}:00",
                  callback_data=rc.edit("quiet_hours.quiet_hours_end"))
        rows.append(2)
    ra = s.get_bool("resource_alerts.enabled", True)
    kb.button(text=f"{_chk(ra)} Алерты хоста", callback_data=rc.toggle("resource_alerts.enabled"))
    rows.append(1)
    if ra:
        kb.button(text=f"CPU {s.get_int('resource_alerts.thresholds_percent.cpu', 80)}%",
                  callback_data=rc.edit("resource_alerts.thresholds_percent.cpu"))
        kb.button(text=f"RAM {s.get_int('resource_alerts.thresholds_percent.ram', 80)}%",
                  callback_data=rc.edit("resource_alerts.thresholds_percent.ram"))
        kb.button(text=f"Диск {s.get_int('resource_alerts.thresholds_percent.disk', 80)}%",
                  callback_data=rc.edit("resource_alerts.thresholds_percent.disk"))
        if temp:
            kb.button(text=f"{s.get_int('app.gateway.temp_alert_c', 75)} °C",
                      callback_data=rc.edit("app.gateway.temp_alert_c"))
            rows += [2, 2]
        else:
            rows.append(3)
    return rows


def backup_kb(rc: RoleCB, encryption: bool = False) -> InlineKeyboardMarkup:
    """Автобэкапы тумблером и «🔐 Шифрование» — всегда (фраза нужна и для
    восстановления шифрованных копий); включены — канал циклом, день и час
    одной кнопкой, «сделать сейчас»."""
    s = settings
    kb = InlineKeyboardBuilder()
    on = s.get_bool("app.scheduler.backup_enabled", True)
    kb.button(text=f"{_chk(on)} Автобэкапы", callback_data=rc.toggle("app.scheduler.backup_enabled"))
    kb.button(text="🔐 Шифрование", callback_data=rc.do("encryption"))
    rows = [2]
    if on:
        ch = str(s.get("app.scheduler.backup_channel", "telegram") or "telegram").lower()
        kb.add(rc.cycle_button("app.scheduler.backup_channel",
                               "📨 Куда: " + ("E-mail" if ch == "email" else "Telegram")))
        kb.button(text=backup_when_label(s.get_int("app.scheduler.backup_day", 1),
                                         s.get_int("app.scheduler.backup_hour", 12)),
                  callback_data=rc.edit("backup_when"))
        kb.button(text="💾 Сделать сейчас", callback_data=rc.do("backup_now"))
        rows += [2, 1]
    kb.adjust(*rows)
    kb.row(rc.back_button("root"))
    return kb.as_markup()


def encryption_kb(rc: RoleCB, has_secret: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Сменить фразу" if has_secret else "🔑 Задать фразу", callback_data=rc.do("encryption_set"))
    kb.add(rc.back_button("backup"))
    kb.adjust(1)
    return kb.as_markup()


def email_kb(rc: RoleCB, configured: bool, extra=None) -> InlineKeyboardMarkup:
    """Почтовый канал: подключить ящик; настроен — проверка и тест, смена и
    отключение; extra(kb, rows) — ряды, которые есть только у роли."""
    kb = InlineKeyboardBuilder()
    rows: list[int] = []
    if not configured:
        kb.button(text="✉️ Подключить ящик", callback_data=rc.do("email_setup"))
        rows.append(1)
    else:
        kb.button(text="🔍 Проверить", callback_data=rc.do("email_check"))
        kb.button(text="📨 Тест-письмо", callback_data=rc.do("email_test"))
        kb.button(text="✏️ Сменить ящик", callback_data=rc.do("email_setup"))
        kb.button(text="🗑 Отключить", callback_data=rc.do("email_forget"))
        rows += [2, 2]
        if extra is not None:
            extra(kb, rows)
    kb.adjust(*rows)
    kb.row(rc.back_button("root"))
    return kb.as_markup()


def email_offer_kb(rc: RoleCB, back_sec: str) -> InlineKeyboardMarkup:
    """«Почта не настроена» — назад в раздел или настроить сейчас."""
    kb = InlineKeyboardBuilder()
    kb.add(rc.back_button(back_sec))
    kb.button(text="✉️ Настроить почту", callback_data=rc.do("email_setup"))
    kb.adjust(2)
    return kb.as_markup()


def updates_kb(rc: RoleCB, muted: bool, target_tag: str = "", blocked: str = "") -> InlineKeyboardMarkup:
    """target_tag — найденная цель обновления (кнопка «⬆️ Обновить до vX»,
    если не заблокирована); «Уведомлять» — мьют в БД; «Проверка» — цикл
    день → неделя → месяц."""
    from awgbot.bot.callbacks import UpdateCB
    s = settings
    kb = InlineKeyboardBuilder()
    rows = []
    if target_tag and not blocked:
        tag = target_tag if str(target_tag).startswith("v") else f"v{target_tag}"
        kb.button(text=f"⬆️ Обновить до {tag}", callback_data=UpdateCB(action="install"))
        rows.append(1)
    sched = str(s.get("updates.poll_schedule", "day")).lower()
    if sched not in UPDATE_SCHEDULE_LABELS:
        sched = "month"
    kb.button(text=f"{_chk(not muted)} Уведомлять", callback_data=rc.do("updates_notify"))
    kb.add(rc.cycle_button("updates.poll_schedule", f"📅 Проверка: {UPDATE_SCHEDULE_LABELS[sched]}"))
    rows.append(2)
    kb.adjust(*rows)
    kb.row(rc.back_button("root"))
    return kb.as_markup()


def port_finisher_kb(rc: RoleCB) -> InlineKeyboardMarkup:
    """Финишер «порт не изменился / не выполнена»: другой порт или назад в
    раздел; нажатие оставляет финишер с одной «Скрыть»."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Другой порт", callback_data=rc.do("port_retry"))
    kb.button(text="⬅️ Назад", callback_data=rc.do("port_back"))
    kb.adjust(2)
    return kb.as_markup()
