"""activation.py — часть пакета client (см. __init__): активация, помощь, коды, отсрочка."""

from __future__ import annotations

from awgbot.bot import ui


# ─────────────────────────────────────────────────────────────────────────────
# Активация, помощь, коды
# ─────────────────────────────────────────────────────────────────────────────

# эмодзи и название экрана — как у кнопки, которая его открывает
HELP_INTRO = ui.head("❓ Как подключить") + " — какое у тебя устройство?"
ACTIVATION_OK = "🎉 Доступ открыт"
ACTIVATION_OK_HELP = "🎉 Доступ открыт. Какое у тебя устройство?"
ACTIVATION_INVALID = "🤔 Такого кода нет — проверь его и отправь <code>/code КОД</code>"
ACTIVATION_ALREADY = "ℹ️ У тебя уже есть доступ — меню ниже"
OWN_DEVICE_CODE = "ℹ️ Это приглашение на твоё же устройство — принимать его не нужно 🙂"

# гостю устройства не завести самому — у него своё действие
GUEST_NO_DEVICES_TOAST = "Устройств нет — попроси у друга новое приглашение"

COLD_START_GREETING = ("👋 Не узнаю тебя. Пришёл по приглашению, а код не подхватился? "
                       "Отправь: <code>/code КОД</code>")

CODE_NO_ARG = "Отправь код после команды: <code>/code КОД</code>"


UNMANAGED_DEVICE_DIALOG = (
    "✳️ Добавлено не ботом — ссылки нет: удали и добавь заново"
)


def issue_failed(error) -> str:
    """Сервер не выдал настройки устройства: причина и следующий шаг."""
    from ..fmt import _e
    return f"⚠️ Не удалось получить настройки: {_e(str(error))} — попробуй через минуту"


def limit_changed_notice(old: int, new: int) -> str:
    def _fmt(v):
        return "∞" if v == 0 else str(v)
    return f"Лимит устройств изменён: {_fmt(old)} → {_fmt(new)}"


# ── отсрочка ─────────────────────────────────────────────────────────────────

def grace_activated_client(days: int, end) -> str:
    """end — дата окончания уже строкой экрана."""
    return f"✅ Отсрочка взята: подписка до {end} — {days} дн. вычтутся из следующего продления"


def grace_taken_toast(days: int) -> str:
    return f"Отсрочка: +{days} дн."


GRACE_STALE = "Это предложение уже неактуально"


def grace_activated_admin(link: str, days: int) -> str:
    """link — имя профиля ссылкой (texts.profile_link)."""
    return f"🙏 {link}: взята отсрочка на {days} дн."
