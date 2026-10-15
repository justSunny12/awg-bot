"""Объявления пользователям: адресаты с тумблером продления, дни пресетами, подтверждение."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from awgbot.bot import ui
from awgbot.bot.callbacks import BroadcastCB, PresetCB
from awgbot.bot import texts as _texts

from .common import BTN_CANCEL, confirm


def broadcast_targets(clients, selected, *, extend: bool = False, page: int = 0) -> InlineKeyboardMarkup:
    """Один экран: тумблер «С продлением подписки», «Все профили» по правилу
    массового выбора (☑️ пока не все, ✅ когда все — нажатие снимает всех),
    профили с отметками (с продлением — и состояние подписки), отмена и
    дальше. Набор отмеченных живёт в FSM: лимит колбэка 64 байта."""
    ids = [c.id for c in clients]
    all_on = bool(ids) and all(i in selected for i in ids)

    def _entry(_i, c):
        mark = "✅" if c.id in selected else "☑️"
        sub = _texts.subscription_mark(c) if extend else ""
        return (f"{mark} {c.name}{sub}", BroadcastCB(action="tgl", ref=c.id))
    return ui.rows(
        (("✅" if extend else "☑️") + " С продлением подписки", BroadcastCB(action="ext")),
        (("✅" if all_on else "☑️") + " Все профили", BroadcastCB(action="all")),
        *ui.paged(clients, page, static=3, screen="bcast", ref=0,
                  back=BroadcastCB(action="targets").pack(), button=_entry),
        [("⬅️ Отмена", BroadcastCB(action="cancel")), ("➡️ Далее", BroadcastCB(action="next"))])


def broadcast_days_kb() -> InlineKeyboardMarkup:
    """Дни продления пресетами; «✏️ Другое» — ввод; выход последним рядом:
    «⬅️ Назад» — к адресатам, «✖️ Отмена» — из объявления."""
    return ui.rows(
        [(f"{d} дн.", PresetCB(kind="bc_days", val=d)) for d in (1, 3, 7)],
        ("✏️ Другое", PresetCB(kind="bc_days", val=-1)),
        [ui.back(BroadcastCB(action="targets")), (BTN_CANCEL, BroadcastCB(action="cancel"))])


def broadcast_cancel() -> InlineKeyboardMarkup:
    """«✖️ Отмена» под приглашением к вводу текста или дней — выход из объявления."""
    return ui.rows((BTN_CANCEL, BroadcastCB(action="cancel")))


def broadcast_confirm() -> InlineKeyboardMarkup:
    return confirm(BroadcastCB(action="cancel"), "📢 Отправить", BroadcastCB(action="send"), danger=False)
