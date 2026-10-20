"""reserve.py — клавиатуры раздела «🛟 Резерв»: раздел без пары, приглашение
(живое сообщение), ввод строки и кода, диалог принятия, карточка резерва,
подтверждения."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from awgbot.bot import ui
from awgbot.bot.callbacks import PeerCB, RsvCB, SetCB
from awgbot.bot.keyboards import common as kc


def _chk(on: bool) -> str:
    return "✅" if on else "☑️"


def _back(sec_to: str = "root"):
    return ui.back(SetCB(sec=sec_to))


def section(ready: bool, *, invite_live: bool, accepting: bool) -> InlineKeyboardMarkup:
    """Раздел без пары: готовность, пригласить / принять, запись DNS, назад."""
    busy = invite_live or accepting
    return ui.rows(
        (f"{_chk(ready)} Готов быть резервом: {'вкл' if ready else 'выкл'}", RsvCB(action="ready")),
        ("📤 Пригласить", RsvCB(action="invite")) if not busy else None,
        ("📥 Принять приглашение", RsvCB(action="accept")) if not busy else None,
        ("⬅️ Отменить приглашение", RsvCB(action="cancel")) if busy else None,
        _back())


def invite_message(pid: str) -> InlineKeyboardMarkup:
    return ui.rows([("✏️ Ввести код", RsvCB(action="code", pid=pid)),
                    ("⬅️ Отменить", RsvCB(action="cancel", pid=pid))])


def code_prompt() -> InlineKeyboardMarkup:
    """«✖️ Отмена» под приглашением кода — ввод сброшен, на месте — раздел."""
    return kc.cancel_input("set_rsv", 0)


def accept_dialog(rf_on: bool, *, has_gateway: bool) -> InlineKeyboardMarkup:
    """«Отмена» первой: принять чужих клиентов — действие с последствиями."""
    return ui.rows(
        ("⬅️ Отмена", SetCB(sec="rsv")),
        (f"{_chk(rf_on)} РФ-доступ через мои шлюзы", RsvCB(action="rf")) if has_gateway else None,
        ("🛟 Подключиться", RsvCB(action="connect")))


def accept_message() -> InlineKeyboardMarkup:
    return ui.rows(("⬅️ Отменить", RsvCB(action="cancel")))


def final() -> InlineKeyboardMarkup:
    """Итог в живом сообщении: кнопок нет, остаётся след в чате."""
    return ui.rows()


def card(*, ready: bool, rf_on: bool, has_gateway: bool, ret_immediate: bool) -> InlineKeyboardMarkup:
    return ui.rows(
        (f"{_chk(ready)} Готов быть резервом", RsvCB(action="ready")),
        (f"{_chk(rf_on)} РФ-доступ его клиентов через мои шлюзы", RsvCB(action="rfmine")) if has_gateway else None,
        ("⏱ Возврат: сразу" if ret_immediate else "⏱ Возврат: по кнопке", RsvCB(action="ret")),
        ("🆘 На крайний случай", RsvCB(action="emerg")),
        ("🗑 Удалить резерв", RsvCB(action="unpair")),
        _back())


def guest_card(*, can_return: bool) -> InlineKeyboardMarkup:
    """Карточка резерва в боте основы на резерве: вернуть клиентов (когда
    основа снова в строю и рычаг «по кнопке»), обновить, назад."""
    return ui.rows(
        ("↩️ Вернуть клиентов", RsvCB(action="back")) if can_return else None,
        ("🔄 Обновить", RsvCB(action="refresh")),
        _back())


def emergency(has_code: bool) -> InlineKeyboardMarkup:
    return ui.rows(
        [("🔑 Показать код", RsvCB(action="show_code")) if has_code else None,
         ("🔄 Сменить код", RsvCB(action="new_code")) if has_code else None],
        _back("rsv"))


def confirm_new_code() -> InlineKeyboardMarkup:
    return kc.confirm(RsvCB(action="emerg"), "🔄 Сменить", RsvCB(action="new_code_yes"))


def confirm_unpair() -> InlineKeyboardMarkup:
    return kc.confirm(SetCB(sec="rsv"), "🗑 Удалить", RsvCB(action="unpair_yes"))


def confirm_ready_off() -> InlineKeyboardMarkup:
    return kc.confirm(SetCB(sec="rsv"), "☑️ Выключить", RsvCB(action="ready_off_yes"))


def peer_admin(*, code_needed: bool) -> InlineKeyboardMarkup:
    """Экран роли админа второго сервера: обновить, ввод кода (только в Н4), скрыть."""
    from awgbot.bot.keyboards.common import hide_only
    rows = ui.rows(("🔄 Обновить", PeerCB(action="refresh")),
                   ("🔑 Ввести аварийный код", PeerCB(action="code")) if code_needed else None)
    hide = hide_only().inline_keyboard
    return InlineKeyboardMarkup(inline_keyboard=rows.inline_keyboard + hide)
