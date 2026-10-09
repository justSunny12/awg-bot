"""«✉️ E-mail»: ящик и серверы, проверка и тест-письмо, смена и отключение;
у роли с аварийным выходом из паузы — ещё его тумблер, адрес и циклы опроса
и длины кода. Мастер подключения — handlers/mailwizard, шаги проверки и
отключения — settingscore.email_action."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from awgbot.bot import texts
from awgbot.bot import ui
from awgbot.bot.handlers.common import call
from awgbot.core import settings

from ._kb import _chk, back_button
from .base import Confirm

ID, LABEL, BACK = "email", "✉️ E-mail", "root"
KEYS = ("email.resume_enabled", "email.resume_address", "email.poll_interval_sec", "email.resume_code_len")
BOUNDS = {
    "email.poll_interval_sec": (60, 3600, "Опрос почты", "с"),
    "email.resume_code_len": (6, 16, "Длина кода", "символов"),
}
EMAIL_POLL_CYCLE = (60, 300, 900)          # опрос ящика: 1 → 5 → 15 мин
EMAIL_CODE_CYCLE = (6, 8, 12)              # длина кода аварийного выхода
CYCLES = {"email.poll_interval_sec": EMAIL_POLL_CYCLE, "email.resume_code_len": EMAIL_CODE_CYCLE}


def email_poll_label(seconds: int) -> str:
    return f"⏱ Опрос: {max(60, int(seconds)) // 60} мин"


def email_code_label(n: int) -> str:
    n = int(n)
    word = "символа" if n in (2, 3, 4) else "символов"
    return f"🔢 Код: {n} {word}"


def keyboard(br, configured: bool) -> InlineKeyboardMarkup:
    s = settings
    if not configured:
        return ui.rows(("✉️ Подключить ящик", br.cb.pack(ID, "do", "setup")), back_button(br))
    resume = br.has.email_resume
    on = resume and s.get_bool("email.resume_enabled", True)
    return ui.rows(
        [("🔍 Проверить", br.cb.pack(ID, "do", "check")), ("📨 Тест-письмо", br.cb.pack(ID, "do", "test"))],
        [("✏️ Сменить ящик", br.cb.pack(ID, "do", "setup")), ("🗑 Отключить", br.cb.pack(ID, "do", "forget"))],
        (f"{_chk(on)} Аварийный выход", br.cb.pack(ID, "toggle", "email.resume_enabled")) if resume else None,
        [("✉️ Адрес для кода", br.cb.pack(ID, "edit", "email.resume_address")),
         (email_poll_label(s.get_int("email.poll_interval_sec", 60)), br.cb.pack(ID, "cycle", "email.poll_interval_sec"))] if on else None,
        (email_code_label(s.get_int("email.resume_code_len", 8)), br.cb.pack(ID, "cycle", "email.resume_code_len")) if on else None,
        back_button(br))


def offer_kb(br, back_sec: str) -> InlineKeyboardMarkup:
    """«Почта не настроена» — назад в раздел или настроить сейчас."""
    return ui.rows([back_button(br, back_sec), ("✉️ Настроить почту", br.cb.pack(ID, "do", "setup"))])


async def screen(br, services, key: str = ""):
    acc = await call(services.email_account)
    last = await call(services.email_last_check)
    resume_on = settings.get_bool("email.resume_enabled", True) if br.has.email_resume else None
    addr = await call(services.email_resume_address) if br.has.email_resume else ""
    return texts.settings_email_text(acc, last, br, resume_on, addr), keyboard(br, acc is not None)


async def _email(ctx, key: str):
    from awgbot.bot.handlers import settingscore as core
    await core.email_action(ctx.cb, ctx.services, ctx.hooks, ctx.state, key)


async def _setup(ctx):
    await _email(ctx, "setup")


async def _check(ctx):
    await _email(ctx, "check")


async def _test(ctx):
    await _email(ctx, "test")


async def _forget_question(ctx) -> str:
    return texts.email_forget_confirm(ctx.br)


async def _forget(ctx):
    await _email(ctx, "forget!")


ACTIONS = {"setup": _setup, "check": _check, "test": _test,
           "forget": Confirm(question=_forget_question, label="🗑 Отключить", run=_forget)}
