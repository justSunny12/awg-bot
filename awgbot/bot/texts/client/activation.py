"""activation.py — часть пакета client (см. __init__): активация, помощь, коды, отсрочка."""

from __future__ import annotations

from awgbot.bot import ui


# ─────────────────────────────────────────────────────────────────────────────
# Активация, помощь, коды
# ─────────────────────────────────────────────────────────────────────────────

HELP_INTRO = ui.head("❓ Помощь") + " — какое устройство?"
ACTIVATION_OK = "🎉 Доступ открыт"
ACTIVATION_OK_HELP = "🎉 Доступ открыт. Какое у тебя устройство?"
ACTIVATION_INVALID = "🤔 Такого кода нет — проверь и пришли ещё раз"
ACTIVATION_ALREADY = "У тебя уже есть доступ"

COLD_START_GREETING = ("👋 Не узнаю тебя. Пришёл по приглашению, а код не подхватился? "
                       "Отправь: <code>/code КОД</code>")

CODE_NO_ARG = "Отправь код после команды: <code>/code КОД</code>"


UNMANAGED_DEVICE_DIALOG = (
    "✳️ Добавлено не ботом — ссылки нет: удали и добавь заново"
)


def limit_changed_notice(old: int, new: int) -> str:
    def _fmt(v):
        return "∞" if v == 0 else str(v)
    return f"Лимит устройств изменён: {_fmt(old)} → {_fmt(new)}"


# ── отсрочка ─────────────────────────────────────────────────────────────────

def grace_activated_client(days: int, end) -> str:
    """end — дата окончания уже строкой экрана."""
    return f"🙏 Продлено на {days} дн., до {end} — вычтется из следующего продления"


GRACE_STALE = "Это предложение уже неактуально"


def grace_activated_admin(link: str, days: int) -> str:
    """link — имя профиля ссылкой (texts.profile_link)."""
    return f"🙏 {link}: взята отсрочка на {days} дн."
