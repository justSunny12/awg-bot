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
        return ui.head("💾 Бэкапы", meta=["🔓 ключевая фраза не задана"]) + "\n" + nb.no_phrase_line(has_slots)
    lines = [ui.head("💾 Бэкапы", ui.tick(on) + (" вкл" if on else " выкл"), meta=["🔐 ключевая фраза задана"])]
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
    """Отказ словами осмотра; команда терминала — моноширинным."""
    from awgbot.domain.backup.inspect import TERMINAL
    return f"⚠️ Файл не принят: {_e(error)}".replace(_e(TERMINAL), f"<code>{_e(TERMINAL)}</code>")


RESTORE_ALREADY = "♻️ Уже восстанавливаю — бот перезапустится и пришлёт отчёт"
RELAY_RESTORING = "сервер AWG уже восстанавливается из бэкапа — дождись окончания"


def relay_source(ref: str) -> str:
    """Строка над планом: бэкап пришёл не из этого чата, а через шлюз."""
    return f"📥 Бэкап получен от шлюза {ref}"


def relay_ask_failed(reason: str) -> str:
    """Бэкап годится, а вопрос в чат бота сервера не ушёл (Telegram) — причина для
    чата шлюза; ссылку на чат бота сервера ставит агент (он знает бота из role)."""
    return (f"файл бэкапа подходящий, но вопрос в чат бота сервера AWG не отправлен: {reason} — пришли бэкап "
            "в чат бота сервера")
RESTORE_REPLACED = "Не актуально: прислан новый файл бэкапа"            # финишер прежнего вопроса (stale_finisher)
RESTORE_OTHER_FILE = "Эта кнопка — от прежнего файла: восстанавливай из последнего вопроса"


def restore_failed(reason: str) -> str:
    """Восстановление из чата не прошло — ничего не изменилось."""
    return f"⚠️ Восстановление из бэкапа не удалось: {_e(reason)}"


RESTORE_STARTED = "♻️ Восстанавливаю из бэкапа — бот перезапустится и через несколько секунд пришлёт отчёт"


def restore_done_report(created_at_iso: str, report: dict, bot_username: str = "", *, version: str = "") -> str:
    """Отчёт после восстановления: итог по шлюзам и устройства, которые не встали;
    version — восстановление шло с переходом на выпуск бэкапа (Т-В3)."""
    from awgbot.domain.notices import backup as nb
    text = nb.restore_report(created_at_iso, report or {}, bot_username=bot_username)
    if version:
        from awgbot.domain.notices.fleet import restore_version_done
        head, _, tail = text.partition("\n")
        text = head + "\n" + restore_version_done(version) + ("\n" + tail if tail else "")
    return text


def email_not_configured(sec: str = "notify") -> str:
    """Ящика нет, а он нужен: вопрос называет, зачем; кнопка «✉️ Подключить
    ящик» стоит рядом — путь по меню не нужен."""
    if sec == "backup":
        return "✉️ Чтобы бэкапы уходили на e-mail, нужен подключённый ящик. Подключить сейчас?"
    return "✉️ Для аварий на e-mail нужен подключённый ящик. Подключить сейчас?"


def backup_needs_encryption(br=None) -> str:
    return ("По почте уходят только шифрованные копии: в базе приватные ключи устройств. Задай ключевую фразу "
            "в «🔐 Шифрование»")


def backup_encryption_text(mode: str, br) -> str:
    """Экран «Шифрование»: состояние и правила; «вне сервера» — роли."""
    state = "🔐 ключевая фраза задана" if mode == "passphrase" else "🔓 ключевая фраза не задана — бэкапы не делаются"
    return (ui.head("🔐 Шифрование бэкапов", state) + "\n"
            f"Ключевую фразу знаешь только ты — храни вне {br.host_gen}, без неё бэкап не открыть"
            + details("Бот принимает ключевую фразу сообщением, тут же удаляет и никогда не показывает обратно. "
                      "Смена ключевой фразы не перешифровывает старые бэкапы: из чата бот расшифровывает только "
                      "текущей фразой, а из бэкапа с прежней фразой можно восстановиться в терминале сервера — "
                      "<code>sudo awg-bot restore &lt;файл&gt;</code>. Не теряй прежнюю фразу, пока такие "
                      "бэкапы нужны"))


BACKUP_ASK_PASSPHRASE = ui.prompt("🔐 Ключевая фраза", "не короче 8 символов; сообщение удалю сразу после приёма")
BACKUP_ASK_PASSPHRASE_AGAIN = "Повтори ключевую фразу ещё раз — так исключим опечатку"
BACKUP_PASSPHRASE_MISMATCH = "⚠️ Ключевые фразы не совпали — начнём заново: пришли ключевую фразу"
BACKUP_PASSPHRASE_SET = "✅ Ключевая фраза задана — следующие копии уйдут шифрованными"


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


def backup_not_sent(reason: str) -> str:
    """«Сделать сейчас»: файл собран, а Telegram его не принял — сказать, почему."""
    return f"⚠️ Бэкап не отправлен в чат: {_e(reason)}"


def backup_mailed(address: str, n: int = 1) -> str:
    return f"📨 Бэкап отправлен на <code>{_e(address)}</code>"
