"""Общие элементы: reply-клавиатура, маркеры состояния, выбор периода, да/нет, блокировки."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup,
    ReplyKeyboardRemove)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from awgbot.core import config
from awgbot.core import blocks as _blocks
from awgbot.bot.callbacks import BlockCB, ClientCB, ConfirmCB, Menu, PeriodCB, HideCB
from awgbot.bot import texts as _texts


# ─────────────────────────────────────────────────────────────────────────────
# Reply-клавиатура (глобальные команды у поля ввода): «Меню» и «Отмена».
# Тексты кнонок — точные строки, по ним ловим в приоритетном роутере
# reply_commands. Эмодзи-префикс делает случайное совпадение с вводом
# (имя устройства и т.п.) практически невозможным.
# ─────────────────────────────────────────────────────────────────────────────

BTN_CANCEL = "\u2716\ufe0f Отмена"  # ✖️ Отмена


def reply_cancel() -> ReplyKeyboardMarkup:
    """Кнопка «Отмена» у поля ввода — на время текстового ввода."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True, is_persistent=True)


def reply_hide() -> ReplyKeyboardRemove:
    """Убрать reply-клавиатуру (когда открыто главное меню)."""
    return ReplyKeyboardRemove()


# ── Маркеры состояния и суффиксы для кнопок ────────────────────────────────

def _chk(on: bool) -> str:
    return "🟢" if on else "🔴"


def _tick(on: bool) -> str:
    """Галочка для СПИСКОВ-перечислений: кому разрешено, о чём уведомлять.

    Кружок оставлен переключателям состояния сервиса («функция включена»), а в
    перечислениях он читался как «профиль жив / профиль лежит» — то есть как
    состояние того, что перечислено, а не как отметка выбора. Галочка та же,
    что в списке устройств маршрутизации и в выборе адресатов рассылки.
    """
    return "✅" if on else "☑️"


def _btn_suffix(dev) -> str:
    """Суффикс имени устройства для КНОПОК (HTML не рендерится): звёздочка у
    устройств, которые бот не создавал. Единый источник для всех списков."""
    return "" if dev.is_managed else " *"


def _dev_emoji(d) -> str:
    """Иконка типа устройства — та же, что в текстовых списках (см.
    texts.device_emoji): своя копия здесь про шлюз и про непринятый инвайт не
    знала."""
    return _texts.device_emoji(d)


# ─────────────────────────────────────────────────────────────────────────────
# Выбор периода (создание/продление)
# ─────────────────────────────────────────────────────────────────────────────

def period_choices(ctx: str, ref: int = 0, min_days: int = 0,
                   cancel_to: str | None = None) -> InlineKeyboardMarkup:
    """ctx: create | extend. ref: id клиента при продлении.
    min_days: скрыть периоды короче/равные (после вычета отсрочки остался бы ноль
    или минус). «never» не отсекается никогда — вычитать из безлимита нечего.
    Минимальные длительности kind'ов берём консервативно (month=28, year=365),
    чтобы гарантированно не показать период, который может оказаться коротким."""
    _MIN_DAYS = {"day": 1, "week": 7, "month": 28, "year": 365}
    kb = InlineKeyboardBuilder()
    n = 0
    for kind in config.PERIOD_CHOICES:
        if kind != "never" and _MIN_DAYS.get(kind, 0) <= min_days:
            continue
        kb.button(text=config.PERIOD_LABELS[kind],
                  callback_data=PeriodCB(kind=kind, ctx=ctx, ref=ref))
        n += 1
    # Кнопка выхода: при продлении — назад к карточке клиента; при создании —
    # отмена в главное меню. Без неё диалог выбора срока — тупик (был баг).
    if cancel_to:                                   # пришли не из карточки
        kb.button(text="⬅️ Отмена", callback_data=cancel_to)
    elif ctx == "extend" and ref:
        kb.button(text="⬅️ Отмена", callback_data=ClientCB(action="open", client_id=ref))
    else:
        kb.button(text="⬅️ Отмена", callback_data=Menu(action="main"))
    # периоды по 2 в ряд, кнопка отмены — отдельной строкой снизу
    rows = [2] * (n // 2) + ([1] if n % 2 else []) + [1]
    kb.adjust(*rows)
    return kb.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Да/Нет
# ─────────────────────────────────────────────────────────────────────────────

def yes_no(action: str, ref: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Да", callback_data=ConfirmCB(action=action, ref=ref, yes=True))
    kb.button(text="Нет", callback_data=ConfirmCB(action=action, ref=ref, yes=False))
    kb.adjust(2)
    return kb.as_markup()


def to_menu() -> InlineKeyboardMarkup:
    """Одна кнопка «В меню» — завершитель под контентом (admin/client)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f В меню", callback_data=Menu(action="main"))
    return kb.as_markup()


def append_hide_row(kb: InlineKeyboardBuilder) -> InlineKeyboardMarkup:
    """Добавляет «Скрыть» ПОСЛЕДНЕЙ строкой к уже собранной клавиатуре и
    возвращает готовую разметку. Используется везде, где у проактивного
    уведомления есть свои кнопки действия (сейчас — только grace_offer)."""
    kb.row(InlineKeyboardButton(text="Скрыть", callback_data=HideCB().pack()))
    return kb.as_markup()


def hide_only() -> InlineKeyboardMarkup:
    """Клавиатура из одной кнопки «Скрыть» — дефолт для проактивных уведомлений
    без собственных кнопок действия (notifier подставляет её автоматически)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="Скрыть", callback_data=HideCB())
    return kb.as_markup()


# ── Ручные блокировки ────────────────────────────────────────────────────────


def _manual_block_button(target: str, ref: int, mask: int, *, for_admin: bool):
    """Кнопка «Заблокировать»/«Разблокировать» для карточки.
    Админ управляет всеми ручными битами → смотрит на всю ручную маску.
    Клиент управляет ТОЛЬКО своим USER-битом → кнопка отражает лишь его: если
    сам заблокировал → «Разблокировать», иначе «Заблокировать». Админские биты
    на его устройстве клиент кнопкой не снимет (и кнопка это не обещает)."""
    if for_admin:
        manual = _blocks.DEVICE_MANUAL if target == "dev" else _blocks.CLIENT_MANUAL
        has_manual = int(mask) & int(manual)
    else:
        user_bit = (_blocks.DeviceBlock.USER if target == "dev"
                    else _blocks.ClientBlock.USER)
        has_manual = int(mask) & int(user_bit)
    if has_manual:
        return ("✅ Разблокировать", BlockCB(target=target, action="menu_unblock", ref=ref))
    return ("🛑 Заблокировать", BlockCB(target=target, action="menu_block", ref=ref))


def block_pause_choice(client_id: int) -> InlineKeyboardMarkup:
    """Блок клиента: приостановить ли подписку на время блокировки."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⏸ Да, приостановить подписку",
              callback_data=BlockCB(target="cli", action="pause_yes", ref=client_id))
    kb.button(text="▶️ Нет, подписка тикает",
              callback_data=BlockCB(target="cli", action="pause_no", ref=client_id))
    kb.button(text="⬅️ Отмена", callback_data=BlockCB(target="cli", action="cancel", ref=client_id))
    kb.adjust(1)
    return kb.as_markup()


def block_notify_choice(target: str, ref: int, pause_days: int = -1) -> InlineKeyboardMarkup:
    """Админ ставит блок: уведомить пользователя или тихо. pause_days — режим
    приостановки (в отдельном поле days): -1 без паузы, 0 бессрочно, N срочная."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔔 С уведомлением",
              callback_data=BlockCB(target=target, action="block", ref=ref, kind="notified", days=pause_days))
    kb.button(text="🔕 Тихо (не уведомлять)",
              callback_data=BlockCB(target=target, action="block", ref=ref, kind="silent", days=pause_days))
    kb.button(text="⬅️ Отмена", callback_data=BlockCB(target=target, action="cancel", ref=ref))
    kb.adjust(1)
    return kb.as_markup()


def block_unblock_reasons(target: str, ref: int, mask: int) -> InlineKeyboardMarkup:
    """Админ снимает блок: перечислить активные РУЧНЫЕ причины + «Снять всё»
    (если больше одной). Если причина ровно одна — этот экран не показываем
    вовсе (см. admin_unblock_menu), снимаем сразу."""
    kb = InlineKeyboardBuilder()
    if target == "dev":
        items = [("silent", _blocks.DeviceBlock.ADMIN_SILENT, "Тихий админ-блок"),
                 ("notified", _blocks.DeviceBlock.ADMIN_NOTIFIED, "Админ-блок"),
                 ("user", _blocks.DeviceBlock.USER, "Блок владельца")]
    else:
        items = [("silent", _blocks.ClientBlock.ADMIN_SILENT, "Тихий админ-блок"),
                 ("notified", _blocks.ClientBlock.ADMIN_NOTIFIED, "Админ-блок"),
                 ("user", _blocks.ClientBlock.USER, "Блок владельца")]
    active = [(kind, lbl) for kind, bit, lbl in items if int(mask) & int(bit)]
    for kind, lbl in active:
        kb.button(text=lbl,
                  callback_data=BlockCB(target=target, action="unblock", ref=ref, kind=kind))
    if len(active) > 1:
        kb.button(text="Снять всё",
                  callback_data=BlockCB(target=target, action="unblock", ref=ref, kind="all"))
    kb.button(text="⬅️ Отмена", callback_data=BlockCB(target=target, action="cancel", ref=ref))
    kb.adjust(1)
    return kb.as_markup()
