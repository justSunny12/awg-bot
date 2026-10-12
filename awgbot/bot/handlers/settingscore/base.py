"""base.py — часть пакета settingscore (см. __init__): крючки роли (Hooks), приглашение к вводу, раздел после ввода."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from awgbot.bot.handlers.common import cleanup_content, send_menu


@dataclass
class Hooks:
    """Крючки роли (собирает sections.hooks_for из словаря роли).

    email_offer_kb(sec)   — «почта не настроена»: назад в sec / настроить;
    render(cb, services, sec) — перерисовать раздел на месте кнопки;
    screen(services, sec) — (text, markup) раздела, для показа новым сообщением;
    main(services, chat_id) — (text, markup) главного экрана роли;
    br                    — словарь роли: тексты про почту и шифрование.
    """
    email_offer_kb: Callable[[str], object]
    render: Callable[[CallbackQuery, object, str], Awaitable[None]]
    screen: Callable[[object, str], Awaitable[tuple]]
    main: Callable[[object, int], Awaitable[tuple]]
    br: object


# Умолчания и множители хранения ключей — DEFAULTS/SCALE модулей разделов
# (sections.default, sections.scale): ядро знает ключ только через раздел.


async def ask(cb: CallbackQuery, services, state: FSMContext, prompt: str, sec: str) -> None:
    """Приглашение к вводу — на месте экрана, в служебные, с «✖️ Отмена»
    реестра экранов (CancelCB): отмена возвращает раздел sec на место
    приглашения и снимает его со служебных — уборка при возврате в меню
    иначе сносила живое меню. Одна схема с остальными приглашениями бота."""
    from awgbot.bot.handlers.common import ask_here
    await ask_here(cb, services, state, prompt, f"set_{sec}")


async def after_input(message: Message, services, hooks: Hooks, sec: str, note: str = "") -> None:
    """Раздел после ТЕКСТОВОГО ввода — новым сообщением через send_menu; итог
    ввода («✅ Частота опроса: 3 → 5 мин») — первой строкой раздела, а не
    отдельным сообщением. Служебное убираем: приглашение, ввод человека,
    переспросы (всё это трекается)."""
    from awgbot.bot.screens import with_note
    await cleanup_content(message.bot, services, message.chat.id)
    text, markup = await hooks.screen(services, sec)
    await send_menu(message, services, with_note(text, note), markup)
