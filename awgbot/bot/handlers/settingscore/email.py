"""email.py — часть пакета settingscore (см. __init__): действия раздела почты."""
from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from awgbot.bot import ui
from awgbot.bot import texts
from awgbot.bot.states import EmailSetup
from awgbot.bot.handlers.common import call
from .base import Hooks, ask


# ── почта ────────────────────────────────────────────────────────────────────

async def _render_with_note(cb: CallbackQuery, services, hooks: Hooks, sec: str, note: str) -> None:
    """Раздел на месте кнопки с итогом первой строкой."""
    from awgbot.bot.handlers.common import edit
    from awgbot.bot.screens import with_note
    text, markup = await hooks.screen(services, sec)
    await edit(cb, with_note(text, note), markup)


async def email_action(cb: CallbackQuery, services, hooks: Hooks, state: FSMContext,
                       key: str) -> bool:
    """setup | check | test | forget! (вопрос «отключить?» — Confirm раздела). False — ключ не наш."""
    from awgbot.infra import mail
    if key == "setup":
        await state.clear()
        await state.set_state(EmailSetup.address)
        acc = await call(services.email_account)
        prompt = texts.email_ask_address_change(acc.login) if acc else texts.EMAIL_ASK_ADDRESS
        await ask(cb, services, state, prompt, "email")
        await cb.answer()
        return True
    if key == "check":
        # колбэк отвечаем сразу (IMAP-вход может идти дольше лимита ответа),
        # итог — первой строкой раздела
        await cb.answer(ui.Toast.checking)
        ok, detail = await call(services.email_check)
        # отказ уже в шапке раздела («✉️ E-mail 🔴 …») — вторая строка была дублем
        await _render_with_note(cb, services, hooks, "email", texts.EMAIL_CHECK_OK if ok else "")
        return True
    if key == "test":
        await cb.answer("Отправляю…")
        try:
            await call(services.email_send_test)
        except mail.MailError as e:
            # действие не выполнено — ⚠️; причина «SMTP — …», как у mail_why в соседних экранах
            from awgbot.domain.notices.gateway import mail_why
            await _render_with_note(cb, services, hooks, "email", f"⚠️ Тест-письмо не ушло: {mail_why(str(e))}")
            return True
        acc = await call(services.email_account)
        await _render_with_note(cb, services, hooks, "email",
                                "✅ " + texts.email_test_sent(acc.login if acc else ""))
        return True
    if key == "forget!":
        await call(services.email_forget)
        await hooks.render(cb, services, "email")
        await cb.answer(texts.EMAIL_FORGOTTEN)
        return True
    return False
