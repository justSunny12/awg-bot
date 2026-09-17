"""Объявления пользователям: режим, адресаты, подтверждение."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from awgbot.bot.callbacks import BroadcastCB
from awgbot.bot import texts as _texts


def broadcast_mode() -> InlineKeyboardMarkup:
    """Первый экран объявления: простое или с продлением подписки."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✉️ Простое", callback_data=BroadcastCB(action="mode", ref=0))
    kb.button(text="💌 С продлением подписки", callback_data=BroadcastCB(action="mode", ref=1))
    kb.button(text="\u2b05\ufe0f Отмена", callback_data=BroadcastCB(action="cancel"))
    kb.adjust(1)
    return kb.as_markup()


def broadcast_targets(clients, selected, *, extend: bool = False) -> InlineKeyboardMarkup:
    """Выбор адресатов: отметки на профилях, «отметить все», «Далее», «Отмена».
    extend — объявление с продлением: справа от имени состояние подписки
    (∞ бессрочная, ⛔ и дата — истекла), чтобы решить, кому и сколько.
    Онлайн-статуса нет намеренно: не онлайн — прочитает потом.

    Мультивыбор, а не по одному профилю за раз: объявление обычно касается
    нескольких сразу, и гонять весь путь ввод-превью-отправка по разу на каждого
    значило бы рассылать один и тот же текст руками N раз.

    Набор отмеченного в callback_data не носим — он живёт в FSM-data: лимит
    Telegram 64 байта на строку, а профилей может быть сколько угодно.
    """
    kb = InlineKeyboardBuilder()
    ids = [c.id for c in clients]
    all_on = bool(ids) and all(i in selected for i in ids)
    kb.button(text="☑️ Снять все" if all_on else "✅ Отметить все",
              callback_data=BroadcastCB(action="all"))
    rows = [1]
    for c in clients:
        mark = "✅" if c.id in selected else "☑️"
        sub = _texts.subscription_mark(c) if extend else ""
        kb.button(text=f"{mark} {c.name}{sub}",
                  callback_data=BroadcastCB(action="tgl", ref=c.id))
        rows.append(1)
    kb.button(text="\u2b05\ufe0f Отмена", callback_data=BroadcastCB(action="cancel"))
    kb.button(text="➡️ Далее", callback_data=BroadcastCB(action="next"))
    kb.adjust(*rows, 2)
    return kb.as_markup()


def broadcast_cancel() -> InlineKeyboardMarkup:
    """«Отмена» через СВОЙ колбэк, а не прямой навигацией в главное меню.

    Он сбрасывает FSM явно. Прежде отмена вела в меню, чей хендлер чистит
    состояние попутно, — работало, но держалось на побочном эффекте соседа:
    передумавший на шаге ввода админ иначе остался бы в состоянии
    Broadcast.text, и следующее его сообщение стало бы черновиком объявления."""
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f Отмена", callback_data=BroadcastCB(action="cancel"))
    return kb.as_markup()


def broadcast_confirm() -> InlineKeyboardMarkup:
    """Отмена слева, отправка справа — как и на выборе адресатов. Необратимое
    действие не должно стоять там, куда палец идёт по инерции."""
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f Отмена", callback_data=BroadcastCB(action="cancel"))
    kb.button(text="📢 Отправить", callback_data=BroadcastCB(action="send"))
    kb.adjust(2)
    return kb.as_markup()
