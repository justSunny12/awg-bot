"""Объявления пользователям: адресаты с тумблером продления, дни пресетами, подтверждение."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from awgbot.bot.callbacks import BroadcastCB, PresetCB
from awgbot.bot import texts as _texts

from .common import page_slice, page_nav, confirm


def broadcast_targets(clients, selected, *, extend: bool = False, page: int = 0) -> InlineKeyboardMarkup:
    """Один экран: тумблер «С продлением подписки», «Выбрать все» по правилу
    массового выбора (☑️ пока не все, ✅ когда все — нажатие снимает всех),
    профили с отметками (с продлением — и состояние подписки), отмена и
    дальше. Набор отмеченных живёт в FSM: лимит колбэка 64 байта."""
    kb = InlineKeyboardBuilder()
    ids = [c.id for c in clients]
    all_on = bool(ids) and all(i in selected for i in ids)
    kb.button(text=("✅" if extend else "☑️") + " С продлением подписки", callback_data=BroadcastCB(action="ext"))
    kb.button(text=("✅" if all_on else "☑️") + " Выбрать все", callback_data=BroadcastCB(action="all"))
    rows = [1, 1]
    chunk, page, prev, nxt = page_slice(clients, page, static=3)
    for _i, c in chunk:
        mark = "✅" if c.id in selected else "☑️"
        sub = _texts.subscription_mark(c) if extend else ""
        kb.button(text=f"{mark} {c.name}{sub}", callback_data=BroadcastCB(action="tgl", ref=c.id))
        rows.append(1)
    nav = page_nav(kb, "bcast", 0, page, prev, nxt, BroadcastCB(action="targets").pack())
    if nav:
        rows.append(nav)
    kb.button(text="⬅️ Отмена", callback_data=BroadcastCB(action="cancel"))
    kb.button(text="➡️ Далее", callback_data=BroadcastCB(action="next"))
    kb.adjust(*rows, 2)
    return kb.as_markup()


def broadcast_days_kb() -> InlineKeyboardMarkup:
    """Дни продления пресетами; «✏️ Другое» — ввод; «⬅️ Назад» — к адресатам,
    «✖️ Отмена» — выход из объявления."""
    kb = InlineKeyboardBuilder()
    for d in (1, 3, 7):
        kb.button(text=f"{d} дн.", callback_data=PresetCB(kind="bc_days", val=d))
    kb.button(text="✏️ Другое", callback_data=PresetCB(kind="bc_days", val=-1))
    kb.button(text="✖️ Отмена", callback_data=BroadcastCB(action="cancel"))
    kb.button(text="⬅️ Назад", callback_data=BroadcastCB(action="targets"))
    kb.adjust(3, 2, 1)
    return kb.as_markup()


def broadcast_cancel() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data=BroadcastCB(action="cancel"))
    return kb.as_markup()


def broadcast_confirm() -> InlineKeyboardMarkup:
    return confirm(BroadcastCB(action="cancel"), "📢 Отправить", BroadcastCB(action="send"), danger=False)


def broadcast_mode() -> InlineKeyboardMarkup:
    """Совместимость: прежний экран режима — теперь тумблер на адресатах."""
    return broadcast_cancel()
