"""backup.py — часть пакета settings (см. __init__): бэкапы и восстановление."""

from __future__ import annotations

from awgbot.bot import ui
from awgbot.util import timeutil
from ..fmt import _e, details


# ── 💾 Бэкапы ────────────────────────────────────────────────────────────────

def settings_backup_text(encryption: bool = False, channel: str = "telegram", br=None, *,
                         has_slots: bool = False, slot_lines=()) -> str:
    """Бэкапы сервера AWG. Без фразы — только предупреждение (раздел держит
    одну кнопку «🔐 Шифрование»): открытых копий нет; с фразой — расписание,
    блок шлюзов и строки по слотам (slot_lines — готовые строки)."""
    from awgbot.core import settings as s
    from awgbot.domain.notices import backup as nb
    on = s.get_bool("app.scheduler.backup_enabled", True)
    if not encryption:
        return ui.head("💾 Бэкапы", meta=["🔓 фраза не задана"]) + "\n" + nb.no_phrase_line(has_slots)
    lines = [ui.head("💾 Бэкапы", ui.tick(on) + (" вкл" if on else " выкл"), meta=["🔐 фраза задана"])]
    if on:
        day, hour = s.get_int("app.scheduler.backup_day", 1), s.get_int("app.scheduler.backup_hour", 12)
        where = "на e-mail" if str(channel or "").lower() == "email" else "в этот чат"
        lines.append(f"Каждое {day}-е число в {timeutil.fmt_hour_ui(hour)} → {where}")
    if has_slots:
        lines.append(nb.GATEWAYS_IN_BACKUP)
        lines.extend(x for x in slot_lines if x)
    lines.append("Восстановить — пришли боту файл бэкапа (.tgz.enc)")
    return "\n".join(lines)


def restore_offer_plan(plan, bot_username: str = "") -> str:
    """Вопрос перед восстановлением — план (тот же текст печатает терминал)."""
    from awgbot.domain.notices import backup as nb
    return nb.restore_plan(plan, bot_username=bot_username)


def restore_rejected(error: str) -> str:
    return f"⚠️ Файл не принят: {_e(error)}"


def restore_failed(reason: str) -> str:
    """Восстановление из чата не прошло — ничего не изменилось."""
    return f"⚠️ Бэкап не восстановлен: {_e(reason)}"


RESTORE_STARTED = "♻️ Восстанавливаю из бэкапа — бот перезапустится и через несколько секунд пришлёт отчёт"


def restore_done_report(created_at_iso: str, report: dict, bot_username: str = "") -> str:
    """Отчёт после восстановления: итог по шлюзам и устройства, которые не встали."""
    from awgbot.domain.notices import backup as nb
    return nb.restore_report(created_at_iso, report or {}, bot_username=bot_username)


def email_not_configured(sec: str = "notify") -> str:
    """Ящика нет, а он нужен: вопрос называет, зачем; кнопка «✉️ Подключить
    ящик» стоит рядом — путь по меню не нужен."""
    if sec == "backup":
        return "✉️ Чтобы бэкапы уходили на e-mail, нужен подключённый ящик. Подключить сейчас?"
    return "✉️ Для аварий на e-mail нужен подключённый ящик. Подключить сейчас?"


def backup_needs_encryption(br=None) -> str:
    return "По почте уходят только шифрованные копии: в базе приватные ключи устройств. Задай фразу в «🔐 Шифрование»"


def backup_encryption_text(mode: str, br) -> str:
    """Экран «Шифрование»: состояние и правила; «вне сервера» — роли."""
    state = "🔐 фраза задана" if mode == "passphrase" else f"🔓 фраза не задана — бэкапы {br.host_gen} не делаются"
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
    return (ui.head("✏️ День и час автобэкапа", meta=[f"сейчас {day}-е, {timeutil.fmt_hour_ui(hour)}"])
            + f"\n\nПришли день месяца (1–28) и час по {timeutil.tz_label()} (0–23) через пробел, например "
              f"<code>5 3</code> — 5-е число, {timeutil.fmt_hour_ui(3)}")


def backup_when_changed(old: tuple[int, int], new: tuple[int, int]) -> str:
    """Итог ввода: «✅ Автобэкап: 1-е, 12:00 → 5-е, 03:00 (UTC+5)» — одна метка
    в конце: повторяющееся время, пояс один."""
    return (f"✅ Автобэкап: {old[0]}-е, {timeutil.fmt_hour_ui(old[1], label=False)} → "
            f"{new[0]}-е, {timeutil.fmt_hour_ui(new[1])}")


BACKUP_WHEN_BAD = "⚠️ Нужны два числа: день месяца 1–28 и час 0–23, например <code>1 12</code>"


def backup_mailed(address: str, n: int = 1) -> str:
    return f"📨 Бэкап отправлен на <code>{_e(address)}</code>"
