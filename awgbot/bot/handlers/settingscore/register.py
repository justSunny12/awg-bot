"""register.py — часть пакета settingscore (см. __init__): регистрация обработчиков сообщений."""
from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.states import BackupPassphrase, SettingsInput
from awgbot.bot.handlers.common import call, ask_tracked
from .base import Hooks, after_input
from .values import receive_value
from .actions import _take_secret_message


# ── регистрация обработчиков сообщений (состояния общие на обе роли) ────────

def register(router, hooks: Hooks, *, default_sec: str = "root") -> dict:
    """Обработчики ввода: значение настройки, парольная фраза (два шага),
    мастер почты. Возвращает их по именам — для тестов."""
    from awgbot.bot.handlers import mailwizard

    @router.message(SettingsInput.value)
    async def receive_value_h(message: Message, state: FSMContext, services):
        await receive_value(message, state, services, hooks, default_sec)

    @router.message(BackupPassphrase.first)
    async def passphrase_first_h(message: Message, state: FSMContext, services):
        from awgbot.domain.backupcrypto import MIN_PASSPHRASE_LEN
        phrase = await _take_secret_message(message)
        if len(phrase) < MIN_PASSPHRASE_LEN:
            await ask_tracked(message, services,
                              f"⚠️ Фраза короче {MIN_PASSPHRASE_LEN} символов. Пришли другую")
            return
        await state.update_data(passphrase=phrase)
        await state.set_state(BackupPassphrase.second)
        await ask_tracked(message, services, texts.BACKUP_ASK_PASSPHRASE_AGAIN,
                          reply_markup=kb.cancel_input("set_backup"))

    @router.message(BackupPassphrase.second)
    async def passphrase_second_h(message: Message, state: FSMContext, services):
        phrase = await _take_secret_message(message)
        first = (await state.get_data()).get("passphrase", "")
        if phrase != first:
            await state.set_state(BackupPassphrase.first)
            await state.update_data(passphrase="")
            await ask_tracked(message, services, texts.BACKUP_PASSPHRASE_MISMATCH,
                              reply_markup=kb.cancel_input("set_backup"))
            return
        await state.clear()
        await call(services.backup_set_passphrase, phrase)
        await after_input(message, services, hooks, "backup", texts.BACKUP_PASSPHRASE_SET)

    async def _email_done(message: Message, services, note: str = ""):
        await after_input(message, services, hooks, "email", note)

    mw = mailwizard.register(router, cancel_kb=lambda: kb.cancel_input("set_email"),
                             done=_email_done)
    return {"receive_value": receive_value_h, "passphrase_first": passphrase_first_h,
            "passphrase_second": passphrase_second_h, **mw}
