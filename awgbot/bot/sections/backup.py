"""«💾 Бэкапы»: автобэкапы тумблером, «🔐 Шифрование» (всегда — фраза нужна и
для восстановления), при включённых — канал циклом, день и час одной
кнопкой, «сделать сейчас»; восстановление из файла в чате (handlers/restore)."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot import ui
from awgbot.bot.handlers.common import call, edit_nav
from awgbot.core import settings

from ._kb import _chk, back_button

ID, LABEL, BACK = "backup", "💾 Бэкапы", "root"
KEYS = ("app.scheduler.backup_enabled", "app.scheduler.backup_channel",
        "app.scheduler.backup_day", "app.scheduler.backup_hour", "backup_when")
BOUNDS = {
    "app.scheduler.backup_day": (1, 28, "День автобэкапа", ""),
    "app.scheduler.backup_hour": (0, 23, "Час автобэкапа", "ч"),
}
CYCLES: dict = {}        # канал — цикл с проверками почты и шифрования (cycle ниже)


def _day_label(day: int) -> str:
    return f"{int(day)}-е"


def backup_when_label(day: int, hour: int) -> str:
    return f"✏️ {_day_label(day)}, {int(hour):02d}:00"


def keyboard(br, encryption: bool = False) -> InlineKeyboardMarkup:
    s = settings
    on = s.get_bool("app.scheduler.backup_enabled", True)
    ch = str(s.get("app.scheduler.backup_channel", "telegram") or "telegram").lower()
    return ui.rows(
        [(f"{_chk(on)} Автобэкапы", br.cb.pack(ID, "toggle", "app.scheduler.backup_enabled")),
         ("🔐 Шифрование", br.cb.pack(ID, "do", "enc"))],
        [("📨 Куда: " + ("E-mail" if ch == "email" else "Telegram"), br.cb.pack(ID, "cycle", "app.scheduler.backup_channel")),
         (backup_when_label(s.get_int("app.scheduler.backup_day", 1), s.get_int("app.scheduler.backup_hour", 12)),
          br.cb.pack(ID, "edit", "backup_when"))] if on else None,
        ("💾 Сделать сейчас", br.cb.pack(ID, "do", "now")) if on else None,
        back_button(br))


def encryption_kb(br, has_secret: bool) -> InlineKeyboardMarkup:
    return ui.rows(("✏️ Сменить фразу" if has_secret else "🔑 Задать фразу", br.cb.pack(ID, "do", "enc_set")),
                   back_button(br, ID))


def restore_confirm_kb(br) -> InlineKeyboardMarkup:
    """«Отмена» первой: восстановление необратимо, промах пальцем не должен
    возвращать всех на неделю назад."""
    return kb.confirm(br.cb.pack(ID, "do", "restore_drop"), "♻️ Восстановить", br.cb.pack(ID, "do", "restore!"))


async def screen(br, services, key: str = ""):
    enc = await call(services.backup_encryption_enabled)
    channel = str(settings.get("app.scheduler.backup_channel", "telegram") or "")
    return texts.settings_backup_text(enc, channel), keyboard(br, enc)


async def cycle(ctx) -> bool:
    """Канал бэкапа — цикл с проверками (ящик, шифрование) в settingscore."""
    if ctx.key != "app.scheduler.backup_channel":
        return False
    from awgbot.bot.handlers import settingscore as core
    cur = str(settings.get(ctx.key, "telegram") or "telegram").lower()
    await core.set_backup_channel(ctx.cb, ctx.services, ctx.hooks, "email" if cur == "telegram" else "telegram")
    return True


async def _enc(ctx):
    """Экран шифрования: состояние и правила роли."""
    mode = await call(ctx.services.backup_encryption_mode)
    await edit_nav(ctx.cb, ctx.services, texts.backup_encryption_text(mode, ctx.br), encryption_kb(ctx.br, bool(mode)))
    await ctx.cb.answer()


async def _enc_set(ctx):
    from awgbot.bot.handlers import settingscore as core
    await core.passphrase_start(ctx.cb, ctx.services, ctx.hooks, ctx.state)


async def _now(ctx):
    from awgbot.bot.handlers import settingscore as core
    await core.backup_now(ctx.cb, ctx.services, ctx.hooks)


async def _restore(ctx):
    from awgbot.bot.handlers import restore as rs
    await rs.run_restore(ctx.cb, ctx.services, ctx.state)


async def _restore_drop(ctx):
    from awgbot.bot.handlers import restore as rs
    await rs.drop_restore(ctx.cb, ctx.state)


ACTIONS = {"enc": _enc, "enc_set": _enc_set, "now": _now, "restore!": _restore, "restore_drop": _restore_drop}
