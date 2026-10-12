"""ui.py — сборщик экрана и атомы, общие для текста и клавиатур обеих ролей.

Правила оформления живут здесь один раз, экраны их не повторяют:
  • заголовок — значок и жирное имя: «🔔 <b>Уведомления</b>»; значок вне <b>;
  • статус — через пробел вплотную к одной сущности («🔀 <b>VPN-транзит</b> 🟢 работает»),
    несколько сущностей — через « · » (meta);
  • всплывашка — без тегов и сущностей, не длиннее 200 единиц UTF-16; повторяющиеся
    строки — таблицей Toast, одиночные живут у своего обработчика;
  • тумблер в тексте — тем же значком, что на его кнопке (✅ / ☑️);
  • подтверждение — «Вопрос?» и цена следующей строкой (confirm), клавиатура —
    keyboards/common.confirm («⬅️ Отмена» первой);
  • итог действия — «✅ сделано» / «🔴 не сделано: причина» (result, fail),
    итог ввода — «✅ Подпись: было → стало» (changed);
  • клавиатура — рядами как есть (rows): ряд — список кнопок, кнопка —
    (подпись, колбэк[, "danger"]); хвосты back/to_menu, сетка grid, листание
    paged. InlineKeyboardBuilder в коде экранов не используется.

Таблица статусов — только значки: слово у каждого состояния своё и
передаётся аргументом st(). Маркеры строк итога — отдельно: ✅ готово,
🔴 не выполнено, ⚠️ предупреждение и переспрос, ⛔ отказ, ℹ️ справка.

Атомы текста (шапка, статус, тумблер, итог, всплывашка) — чистые, без
aiogram, живут в util/textfmt/atoms: их берут и тексты уведомлений домена;
здесь — ре-экспорт под прежними именами (ui.head, ui.tick, ui.Toast…) и
клавиатуры.
"""
from __future__ import annotations

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from awgbot.util.textfmt.atoms import (  # noqa: F401
    STATUS, TOAST_MAX, TEXT_MAX, _TAG, st, tick, head, prompt, label, sub, screen, confirm,
    fail, result, changed, Toast, _u16, toast, note_budget)


# ── клавиатуры ───────────────────────────────────────────────────────────────

BACK_LABEL = "⬅️ Назад"
MENU_LABEL = "⬅️ В меню"


def btn(text: str, cb=None, *, danger: bool = False, url: str | None = None,
        copy: str | None = None) -> InlineKeyboardButton:
    """Кнопка: колбэк (строка или CallbackData), ссылка (url) или копирование
    текста (copy); danger — красная (Bot API 9.x, старые клиенты рисуют обычную)."""
    kw: dict = {}
    if url is not None:
        kw["url"] = url
    elif copy is not None:
        kw["copy_text"] = CopyTextButton(text=copy)
    else:
        kw["callback_data"] = cb if isinstance(cb, str) else cb.pack()
    if danger:
        kw["style"] = "danger"
    return InlineKeyboardButton(text=text, **kw)


def _button(b) -> InlineKeyboardButton:
    if isinstance(b, InlineKeyboardButton):
        return b
    text, cb, *flags = b
    if set(flags) - {"danger"}:
        raise ValueError(f"кнопка «{text}»: неизвестный флаг {flags}; ссылка и копирование — через ui.btn")
    return btn(text, cb, danger="danger" in flags)


def rows(*spec) -> InlineKeyboardMarkup:
    """Ряды кнопок как есть. Ряд — список кнопок; одиночная кнопка (кортеж
    или InlineKeyboardButton) — свой ряд. None вместо кнопки или ряда —
    пропуск, пустой ряд выпадает: условные кнопки пишутся прямо в раскладке."""
    out = []
    for row in spec:
        if row is None:
            continue
        if isinstance(row, (tuple, InlineKeyboardButton)):
            row = [row]
        buttons = [_button(b) for b in row if b is not None]
        if buttons:
            out.append(buttons)
    return InlineKeyboardMarkup(inline_keyboard=out)


def grid(buttons, per: int) -> list[list]:
    """Плоский список кнопок — рядами по per (None пропускаются)."""
    buttons = [b for b in buttons if b is not None]
    return [buttons[i:i + per] for i in range(0, len(buttons), per)]


def back(cb, text: str = BACK_LABEL) -> tuple:
    """Хвост «⬅️ Назад» — на экран cb."""
    return (text, cb)


def to_menu(cb, text: str = MENU_LABEL) -> tuple:
    """Хвост «⬅️ В меню» — на главную роли cb."""
    return (text, cb)


def paged(items, page: int, *, static: int, screen: str, ref: int, back, button) -> list[list]:
    """Ряды списка с листанием: по кнопке на запись (button(index, item) —
    index по ПОЛНОМУ списку) и ряд листания, если страницы есть. static —
    сколько постоянных рядов у экрана кроме списка (правило десяти рядов —
    keyboards/common.page_slice)."""
    from awgbot.bot.keyboards.common import page_nav, page_slice
    chunk, page, prev, nxt = page_slice(items, page, static)
    out = [[button(i, item)] for i, item in chunk]
    nav = page_nav(screen, ref, page, prev, nxt, back)
    if nav:
        out.append(nav)
    return out


__all__ = [
    "STATUS", "TOAST_MAX", "TEXT_MAX", "_TAG", "st", "tick", "head", "prompt", "label", "sub",
    "screen", "confirm", "fail", "result", "changed", "Toast", "_u16", "toast", "note_budget",
    "BACK_LABEL", "MENU_LABEL", "btn", "_button", "rows", "grid", "back", "to_menu", "paged"]
