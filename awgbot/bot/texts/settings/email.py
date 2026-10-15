"""email.py — часть пакета settings (см. __init__): раздел «✉️ E-mail»."""

from __future__ import annotations

from awgbot.bot import ui
from awgbot.util import timeutil
from ..fmt import _e


# ── ✉️ E-mail ────────────────────────────────────────────────────────────────

def settings_email_text(acc, last_check: tuple, br, resume_on=None, resume_addr: str = "") -> str:
    """Экран почтового канала: ящик и серверы одной строкой, состояние в
    заголовке, аварийный выход — строкой (только у роли с has.email_resume;
    resume_on=None — строка не рисуется)."""
    if acc is None:
        return (ui.head("✉️ E-mail", "ящик не подключён") + "\n"
                f"Почта нужна {br.email_purpose}. Портов на {br.host_loc} бот не открывает — сам ходит на почтовый сервер")
    state, iso, detail = last_check
    if state == "ok":
        when = timeutil.age_ago((timeutil.now() - timeutil.parse_iso(iso)).total_seconds()) if iso else ""
        status = f"🟢 <i>{when}</i>" if when else "🟢 проверено"
    elif state == "fail":
        status = f"🔴 {_e(detail)}"
    else:
        status = "⚪ ещё не проверялось"
    lines = [ui.head("✉️ E-mail", status),
             f"<code>{_e(acc.login)}</code> · IMAP <code>{_e(acc.imap_host)}:{acc.imap_port}</code> · SMTP <code>{_e(acc.smtp_host)}:{acc.smtp_port}</code>"]
    if br.has.email_resume and resume_on is not None:
        if resume_on:
            lines.append(f"🆘 Аварийный выход из паузы: код на <code>{_e(resume_addr or acc.login)}</code>")
        else:
            lines.append("🆘 Аварийный выход из паузы выключен")
    return "\n".join(lines)


EMAIL_ASK_ADDRESS = (ui.head("✉️ Подключение ящика") + "\n\n"
                     "Пришли адрес ящика — с него бот будет читать и слать почту. "
                     "Например: <code>box@icloud.com</code>")
EMAIL_ASK_IMAP = ("Домен незнакомый — укажи серверы сам\n"
                  "Пришли IMAP-сервер и порт, например <code>imap.example.com:993</code>")
EMAIL_ASK_SMTP = "Пришли SMTP-сервер и порт, например <code>smtp.example.com:587</code>"
EMAIL_ASK_IMAP_PORT = "Пришли порт IMAP (SSL/TLS) — обычно 993"
EMAIL_ASK_SMTP_PORT = "Пришли порт SMTP (STARTTLS) — обычно 587"
EMAIL_BAD_ADDRESS = "⚠️ Не похоже на адрес почты — пришли адрес вида <code>box@example.com</code>"
EMAIL_BAD_PORT = "⚠️ Порт — число от 1 до 65535, попробуй ещё раз"
EMAIL_BAD_HOST = ("⚠️ Порт — число от 1 до 65535. Пришли сервер и порт, например "
                  "<code>imap.example.com:993</code>")
def email_forget_confirm(br) -> str:
    """Что перестанет работать без почты — у роли своё (email_forget_tail)."""
    return ui.confirm("🗑 Отключить почту?", f"Логин, пароль и серверы будут стёрты; {br.email_forget_tail}")


EMAIL_FORGOTTEN = "✅ Почта отключена"


def email_test_sent(address: str) -> str:
    return f"Письмо ушло на <code>{_e(address)}</code> — проверь входящие"


EMAIL_CHECK_OK = "🟢 Вход по IMAP и SMTP прошёл"


def email_ask_address_change(current: str) -> str:
    return (ui.head("✏️ Смена ящика", meta=[f"сейчас <code>{_e(current)}</code>"]) + "\n"
            "Пришли адрес нового ящика — после проверки входа он заменит текущий; "
            "до этого старый продолжает работать")


def email_ask_resume_address(current: str) -> str:
    return (ui.head("✉️ Адрес для писем с кодом", meta=[f"сейчас <code>{_e(current)}</code>"]) + "\n\n"
            "Сюда клиент на паузе, оставшийся без доступа к Telegram, присылает письмо с кодом. "
            "Обычно это сам ящик или его алиас.\n"
            "Пришли адрес; «-» — вернуть сам ящик")


def email_provider_line(address: str, provider) -> str:
    if provider:
        imap, ip, smtp, sp = provider
        return f"Провайдер распознан: IMAP <code>{_e(imap)}:{ip}</code>, SMTP <code>{_e(smtp)}:{sp}</code>"
    return ""


def email_ask_password(address: str) -> str:
    from awgbot.infra import mail
    hint = mail.PASSWORD_HINTS.get(mail.domain_of(address), "")
    tail = f"\n⚠️ {_e(hint)}" if hint else ""
    return ("Пришли пароль ящика — сообщение с ним удалю сразу; вход по IMAP и SMTP "
            "проверю до сохранения" + tail)


def email_saved(address: str, detail: str) -> str:
    return f"✅ Ящик <code>{_e(address)}</code> подключён" + (f"\n{_e(detail)}" if detail else "")


def email_check_failed(detail: str) -> str:
    return (f"🔴 Ящик не подключён — вход не прошёл: {_e(detail)}\n"
            "Ничего не сохранено — проверь пароль и подключи ещё раз")
