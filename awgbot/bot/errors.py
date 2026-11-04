"""errors.py — отказ словами человеку: один способ на весь бот.

Два слоя:
  • error_text(e) / error_line(e) — что сказать человеку про исключение.
    Отказы проекта (ServiceError и потомки, отказ переезда, отказы слоёв AWG,
    маршрутизации и почты) несут текст, написанный для человека, — он и идёт в чат
    (всплывашка — как есть, строка сообщения — с ⚠️ и экранированием для HTML,
    кроме отказов, размеченных тем, кто бросил). Всё прочее (KeyError,
    TypeError, сбой базы) — «внутренняя ошибка, подробности в журнале»: сырой
    текст исключения человеку не показывается, в журнал — с трассой.
  • on_error — обработчик ошибок диспетчера (dp.errors): обработчик упал —
    человек получает всплывашку (кнопка) или строку (сообщение) вместо
    тишины, журнал — трассу. Страховка, а не замена точечных except: только
    точечный except вернёт живое меню и скажет, что делать дальше.
"""
from __future__ import annotations

import html
import logging

from aiogram.types import ErrorEvent

from awgbot.util.textfmt.atoms import toast_cap

log = logging.getLogger("awgbot.bot.errors")

INTERNAL = "Внутренняя ошибка — подробности записаны в журнал бота"


def _spoken() -> tuple:
    """Исключения с текстом для человека. Лениво: бот не тянет слои при импорте."""
    from awgbot.domain.migration.common import ServiceErrorMigration
    from awgbot.domain.services import ServiceError
    from awgbot.infra.awg import AwgError
    from awgbot.infra.mail import MailError
    from awgbot.infra.routing import RoutingError
    return (ServiceError, ServiceErrorMigration, AwgError, RoutingError, MailError)


def spoken(e: BaseException) -> bool:
    """True — текст исключения написан для человека и его можно показать."""
    return isinstance(e, _spoken())


def error_text(e: BaseException) -> str:
    """Текст без разметки (всплывашка, подпись): отказ проекта — его слова,
    прочее — INTERNAL. Отказ с HTML-разметкой — теги сняты."""
    if not spoken(e):
        return INTERNAL
    text = str(e).strip() or INTERNAL
    if getattr(e, "html", False):
        from awgbot.util.textfmt.atoms import _TAG
        text = html.unescape(_TAG.sub("", text))
    return text


def error_html(e: BaseException) -> str:
    """Текст для HTML-сообщения: экранирован, кроме отказов, размеченных тем,
    кто бросил (ServiceError.html)."""
    if not spoken(e):
        return INTERNAL
    text = str(e).strip() or INTERNAL
    return text if getattr(e, "html", False) else html.escape(text, quote=False)


def error_line(e: BaseException, prefix: str = "⚠️") -> str:
    """Строка итога для сообщения: «⚠️ причина» (⚠️ — действие не выполнено,
    состояние не изменилось; 🔴 — состояние объекта)."""
    return f"{prefix} {error_html(e)}"


def error_toast(e: BaseException) -> str:
    """Всплывашка: текст с заглавной, не длиннее предела Telegram."""
    return toast_cap(error_text(e))


async def on_error(event: ErrorEvent) -> bool:
    """Обработчик ошибок диспетчера: упавший обработчик не оставляет человека без
    ответа. Кнопка — всплывашка (если на колбэк ещё не отвечали — Telegram второй
    ответ отвергает, это не страшно), сообщение — строка «⚠️ …»; отказ проекта —
    его словами и warning в журнал, прочее — INTERNAL и трасса."""
    e = event.exception
    update = event.update
    if spoken(e):
        log.warning("обработчик: отказ без перехвата (%s): %s", type(e).__name__, e)
    else:
        log.error("обработчик упал: %s: %s", type(e).__name__, e, exc_info=e)
    cb = getattr(update, "callback_query", None)
    if cb is not None:
        try:
            await cb.answer(error_toast(e), show_alert=True)
        except Exception as e2:                             # noqa: BLE001 — колбэк уже отвечен или протух
            log.info("обработчик ошибок: всплывашка не ушла: %s", e2)
        return True
    msg = getattr(update, "message", None)
    if msg is not None:
        try:
            await msg.answer(error_line(e))
        except Exception as e2:                             # noqa: BLE001
            log.info("обработчик ошибок: ответ не ушёл: %s", e2)
    return True


__all__ = ["INTERNAL", "spoken", "error_text", "error_html", "error_line", "error_toast", "on_error"]
