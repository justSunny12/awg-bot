"""backup.py — часть пакета settings (см. __init__): бэкапы и восстановление."""

from __future__ import annotations

from awgbot.bot import ui
from awgbot.util import timeutil
from ..fmt import _e, details


# ── 💾 Бэкапы ────────────────────────────────────────────────────────────────

def settings_backup_text(encryption: bool = False, channel: str = "telegram") -> str:
    from awgbot.core import settings as s
    on = s.get_bool("app.scheduler.backup_enabled", True)
    head = ui.head("💾 Бэкапы", ui.tick(on) + (" вкл" if on else " выкл"),
                   meta=["🔐 фраза задана" if encryption else "🔓 без шифрования"])
    lines = [head]
    if on:
        day, hour = s.get_int("app.scheduler.backup_day", 1), s.get_int("app.scheduler.backup_hour", 12)
        where = "на e-mail" if str(channel or "").lower() == "email" else "в этот чат"
        lines.append(f"Каждое {day}-е число в {hour:02d}:00 → {where}")
    lines.append("Восстановить — пришли боту файл бэкапа (.tgz.enc)")
    return "\n".join(lines)


def restore_offer(created_at_iso: str, br, iface_warning: str = "") -> str:
    """Что в копии — у роли своё (backup_contents); iface_warning — цена
    перезапуска AWG (restore_warning), только если восстановление затронет
    интерфейсы."""
    when = timeutil.fmt_dt_ui(timeutil.parse_iso(created_at_iso)) if created_at_iso else "?"
    text = ui.prompt(f"♻️ Бэкап от {when}", "восстановить?") + f"\nВсё вернётся к тому моменту: {br.backup_contents}"
    return text + (f"\n\n{iface_warning}" if iface_warning else "")


def restore_warning(br, carries: bool = True) -> str:
    """Цена перезапуска AWG словами роли — та же, что в подтверждении «🔧 Сервиса»."""
    return br.awg_restart_cost_carrying if carries else br.awg_restart_cost


def restore_rejected(error: str) -> str:
    return f"⚠️ Файл не принят: {_e(error)}"


RESTORE_STARTED = ("♻️ Восстанавливаю из бэкапа: бот остановится, подменит данные и вернётся "
                   "через несколько секунд — отчёт пришлёт уже новый процесс")


def restore_done(created_at_iso: str) -> str:
    when = timeutil.fmt_dt_ui(timeutil.parse_iso(created_at_iso)) if created_at_iso else "?"
    return f"✅ Восстановлено из бэкапа от {when}"


EMAIL_NOT_CONFIGURED = ("✉️ Почта не настроена — нужен подключённый ящик: ⚙️ Настройки → ✉️ E-mail. "
                        "Настроить сейчас?")
def backup_needs_encryption(br) -> str:
    return f"По почте уходят только шифрованные копии: {br.backup_keys}. Задай фразу — 🔐 Шифрование"


def backup_encryption_text(mode: str, br) -> str:
    """Экран «Шифрование»: состояние и правила; «вне сервера» / «вне шлюза» — роли."""
    if mode == "passphrase":
        state = "🔐 фраза задана"
    elif mode == "key":
        state = "🔐 случайный ключ (перенесён из env)"
    else:
        state = "🔓 выключено — копии уходят открытыми и по почте не отправляются"
    return (ui.head("🔐 Шифрование бэкапов", state) + "\n"
            f"Фразу знаешь только ты — храни вне {br.host_gen}, без неё бэкап не открыть"
            + details("Бот принимает фразу сообщением, тут же удаляет и никогда не показывает обратно. "
                      "Смена фразы не перешифровывает старые копии: они открываются прежней — не "
                      "выбрасывай её, пока они нужны"))


BACKUP_ASK_PASSPHRASE = ui.prompt("🔐 Парольная фраза", "не короче 8 символов; сообщение удалю сразу после приёма")
BACKUP_ASK_PASSPHRASE_AGAIN = "Повтори фразу ещё раз — так исключим опечатку"
BACKUP_PASSPHRASE_MISMATCH = "⚠️ Фразы не совпали — начнём заново: пришли фразу"
BACKUP_PASSPHRASE_SET = "✅ Фраза задана — следующие копии уйдут шифрованными"


def backup_when_prompt(day: int, hour: int) -> str:
    return ui.head("✏️ День и час автобэкапа", meta=[f"сейчас {day}-го в {hour:02d}:00",
                                                      "пришли два числа: <code>1 12</code>"])


BACKUP_WHEN_BAD = "⚠️ Нужны два числа: день месяца 1–28 и час 0–23, например <code>1 12</code>"


def backup_mailed(address: str, n: int = 1) -> str:
    return f"📨 Бэкап отправлен на <code>{_e(address)}</code>"
