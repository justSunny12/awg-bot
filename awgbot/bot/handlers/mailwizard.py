"""
mailwizard.py — мастер подключения почтового ящика, общий для обеих ролей.

Шаги: адрес → (серверы, если провайдер незнаком) → пароль → живая проверка →
сохранение. Регистрируется на роутер роли с двумя точками привязки: клавиатура
«Отмена» и экран раздела после завершения. Сообщение с паролем удаляется до
любой проверки — в чате он не остаётся.
"""
from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from awgbot.bot import texts
from awgbot.bot.handlers.common import call
from awgbot.bot.states import EmailSetup
from awgbot.infra import mail


def _host_ok(v: str) -> bool:
    v = v.strip()
    return bool(v) and " " not in v and "." in v


def _port_ok(v: str) -> bool:
    return v.strip().isdigit() and 1 <= int(v.strip()) <= 65535


def register(router, *, cancel_kb, done_screen) -> dict:
    """cancel_kb() → InlineKeyboardMarkup; done_screen(services) → (text, markup)
    (корутина). Возвращает обработчики по именам — для тестов."""

    async def _finish(message: Message, state: FSMContext, services, password: str):
        data = await state.get_data()
        await state.clear()
        acc = mail.MailAccount(login=data["email_address"], password=password,
                               imap_host=data["imap_host"], imap_port=int(data["imap_port"]),
                               smtp_host=data["smtp_host"], smtp_port=int(data["smtp_port"]))
        ok, detail = await call(services.email_check, acc)
        if not ok:
            await message.answer(texts.email_check_failed(detail))
            text, markup = await done_screen(services)
            await message.answer(text, reply_markup=markup)
            return
        await call(services.email_save, acc.login, acc.password, acc.imap_host, acc.imap_port,
                   acc.smtp_host, acc.smtp_port)
        await call(services.email_check)                  # запомнить «проверено сейчас»
        await message.answer(texts.email_saved(acc.login, detail))
        text, markup = await done_screen(services)
        await message.answer(text, reply_markup=markup)

    @router.message(EmailSetup.address)
    async def address(message: Message, state: FSMContext, services):
        addr = (message.text or "").strip()
        if not mail.is_address(addr):
            await message.answer(texts.EMAIL_BAD_ADDRESS)
            return
        await state.update_data(email_address=addr)
        provider = mail.detect_provider(addr)
        if provider:
            imap, ip, smtp, sp = provider
            await state.update_data(imap_host=imap, imap_port=ip, smtp_host=smtp, smtp_port=sp)
            await state.set_state(EmailSetup.password)
            await message.answer(texts.email_provider_line(addr, provider) + "\n\n"
                                 + texts.email_ask_password(addr), reply_markup=cancel_kb())
            return
        await state.set_state(EmailSetup.imap_host)
        await message.answer(texts.EMAIL_ASK_IMAP_HOST, reply_markup=cancel_kb())

    @router.message(EmailSetup.imap_host)
    async def imap_host(message: Message, state: FSMContext):
        v = (message.text or "").strip()
        if not _host_ok(v):
            await message.answer(texts.EMAIL_BAD_HOST); return
        await state.update_data(imap_host=v)
        await state.set_state(EmailSetup.imap_port)
        await message.answer(texts.EMAIL_ASK_IMAP_PORT, reply_markup=cancel_kb())

    @router.message(EmailSetup.imap_port)
    async def imap_port(message: Message, state: FSMContext):
        v = (message.text or "").strip()
        if not _port_ok(v):
            await message.answer(texts.EMAIL_BAD_PORT); return
        await state.update_data(imap_port=int(v))
        await state.set_state(EmailSetup.smtp_host)
        await message.answer(texts.EMAIL_ASK_SMTP_HOST, reply_markup=cancel_kb())

    @router.message(EmailSetup.smtp_host)
    async def smtp_host(message: Message, state: FSMContext):
        v = (message.text or "").strip()
        if not _host_ok(v):
            await message.answer(texts.EMAIL_BAD_HOST); return
        await state.update_data(smtp_host=v)
        await state.set_state(EmailSetup.smtp_port)
        await message.answer(texts.EMAIL_ASK_SMTP_PORT, reply_markup=cancel_kb())

    @router.message(EmailSetup.smtp_port)
    async def smtp_port(message: Message, state: FSMContext):
        v = (message.text or "").strip()
        if not _port_ok(v):
            await message.answer(texts.EMAIL_BAD_PORT); return
        await state.update_data(smtp_port=int(v))
        await state.set_state(EmailSetup.password)
        addr = (await state.get_data()).get("email_address", "")
        await message.answer(texts.email_ask_password(addr), reply_markup=cancel_kb())

    @router.message(EmailSetup.password)
    async def password(message: Message, state: FSMContext, services):
        pw = (message.text or "").strip()
        try:
            await message.delete()                      # пароль в чате не оставляем
        except Exception:                                # noqa: BLE001
            pass
        if not pw:
            await message.answer("⚠️ Пароль пустой. Пришли пароль ещё раз.")
            return
        await _finish(message, state, services, pw)

    return {"address": address, "imap_host": imap_host, "imap_port": imap_port,
            "smtp_host": smtp_host, "smtp_port": smtp_port, "password": password}
