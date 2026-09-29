"""Экран настроек: разделы, почта, бэкапы, сервер, SSH-доступ, границы значений, приглашения и итоги ввода."""

from __future__ import annotations

from awgbot.util import timeutil
from .fmt import _e, _fmt_age, plural_ru, details
from .updates import _ver


# ── корень ───────────────────────────────────────────────────────────────────

def settings_root_text(installed: str | None = None) -> str:
    """«⚙️ Настройки · v3.1.0» — заголовок и версия, больше ничего: разделы
    говорят за себя кнопками."""
    from awgbot.core import config
    return f"⚙️ Настройки · {_e(_ver(installed if installed is not None else config.INSTALLED_VERSION))}"


SETTINGS_ROOT = "⚙️ Настройки"


# ── 🔔 Уведомления ───────────────────────────────────────────────────────────

SETTINGS_NOTIFY_CLIENTS = "👥 О чём сообщать"


def settings_notify_text() -> str:
    """Тихие часы и алерты хоста — строками по текущим значениям; про аварии
    на e-mail — под «подробнее»."""
    from awgbot.core import settings as s
    lines = ["🔔 Уведомления"]
    if s.get_bool("quiet_hours.quiet_hours_enabled", True):
        lines.append(f"Тихие часы {s.get_int('quiet_hours.quiet_hours_start', 20):02d}:00–"
                     f"{s.get_int('quiet_hours.quiet_hours_end', 7):02d}:00 МСК — без звука, кроме аварий")
    else:
        lines.append("Тихие часы выключены — уведомления со звуком круглые сутки")
    if s.get_bool("resource_alerts.enabled", True):
        lines.append(f"Алерты хоста: CPU {s.get_int('resource_alerts.thresholds_percent.cpu', 80)}% · "
                     f"RAM {s.get_int('resource_alerts.thresholds_percent.ram', 80)}% · "
                     f"диск {s.get_int('resource_alerts.thresholds_percent.disk', 80)}%")
    else:
        lines.append("Алерты хоста выключены")
    lines.append(details("Аварии на e-mail — только когда Telegram недоступен: падение VPN-сервиса, "
                         "шлюз, перегруз хоста"))
    return "\n".join(lines)


SETTINGS_NOTIFY = "🔔 Уведомления"


# ── 💳 Подписки ──────────────────────────────────────────────────────────────

def settings_subs_text() -> str:
    from awgbot.core import settings as s
    bonus = s.get_int("limits.traffic_bonus_gb", 100)
    grace = s.get_int("grace.grace_days", 14)
    year = s.get_int("pause.pause_max_total_days", 28)
    month = s.get_int("pause.monthly_pause_days", 2)
    return "\n".join([
        "💳 Подписки — правила для всех",
        f"Бонус {bonus} ГБ при исчерпании · отсрочка {grace} дн.",
        f"Пауза: год +{year} (до {2 * year}), месяц +{month} (до {12 * month})",
        details("Действуют с момента правки и на уже выданные подписки тоже.\n"
                "• Бонус — сколько трафика добавляется профилю разово, когда он упёрся в лимит.\n"
                "• Отсрочка — сколько дней после окончания подписки профиль ещё работает: "
                "истёкший в отпуске или ночью не отваливается молча.\n"
                "• Пауза останавливает срок подписки; неиспользованные дни возвращаются в конце. "
                "Год — сколько дней паузы начисляется годовой подписке при продлении (копится за два "
                "продления); месяц — ежемесячной за своевременное продление (копится за 12). Истечение "
                "или отсрочка лишают бонуса за следующее продление на месяц.\n"
                "Лимиты конкретного профиля — в его карточке"),
    ])


SETTINGS_SUBS = "💳 Подписки — правила для всех"


# ── ✉️ E-mail ────────────────────────────────────────────────────────────────

def settings_email_text(acc, last_check: tuple, resume_on=None, resume_addr: str = "") -> str:
    """Экран почтового канала: ящик и серверы одной строкой, состояние в
    заголовке, аварийный выход — строкой. resume_on=None — у агента шлюза
    аварийного выхода нет, строка не рисуется."""
    if acc is None:
        what = ("для аварийного выхода из паузы по коду в письме, бэкапов и критичных алертов"
                if resume_on is not None
                else "для бэкапов и критичных алертов, когда Telegram недоступен; настройки почты "
                     "приезжают в конфигурации шлюза")
        return ("✉️ E-mail · ящик не подключён\n"
                f"Почта нужна {what}. Портов на хосте не открывается — бот сам ходит на почтовый сервер")
    state, iso, detail = last_check
    if state == "ok":
        when = _fmt_age((timeutil.now() - timeutil.parse_iso(iso)).total_seconds()) if iso else ""
        status = f"🟢 проверено {when}".rstrip()
    elif state == "fail":
        status = f"🔴 {_e(detail)}"
    else:
        status = "⚪ ещё не проверялось"
    lines = [f"✉️ E-mail · {status}",
             f"{_e(acc.login)} · IMAP {_e(acc.imap_host)}:{acc.imap_port} · SMTP {_e(acc.smtp_host)}:{acc.smtp_port}"]
    if resume_on is not None:
        if resume_on:
            lines.append(f"🆘 Аварийный выход из паузы: код на {_e(resume_addr or acc.login)}")
        else:
            lines.append("🆘 Аварийный выход из паузы выключен")
    return "\n".join(lines)


EMAIL_ASK_ADDRESS = ("✉️ Подключение ящика — пришли адрес, от имени которого бот будет читать и "
                     "слать почту: <code>box@icloud.com</code>")
EMAIL_ASK_IMAP = ("Домен незнакомый — укажи серверы сам.\n"
                  "IMAP-сервер и порт: <code>imap.example.com:993</code>")
EMAIL_ASK_SMTP = "SMTP-сервер и порт: <code>smtp.example.com:587</code>"
# прежние четыре шага — на случай старых состояний в памяти диалога
EMAIL_ASK_IMAP_HOST = EMAIL_ASK_IMAP
EMAIL_ASK_IMAP_PORT = "Порт IMAP (SSL/TLS), обычно 993:"
EMAIL_ASK_SMTP_HOST = EMAIL_ASK_SMTP
EMAIL_ASK_SMTP_PORT = "Порт SMTP (STARTTLS), обычно 587:"
EMAIL_BAD_ADDRESS = "⚠️ Не похоже на адрес почты — пришли адрес вида box@example.com"
EMAIL_BAD_PORT = "⚠️ Нужен номер порта от 1 до 65535"
EMAIL_BAD_HOST = "⚠️ Нужно имя сервера и порт: <code>imap.example.com:993</code>"
def email_forget_confirm(gateway: bool = False) -> str:
    """У агента аварийного выхода из паузы нет — по почте у него бэкапы и
    аварийные алерты."""
    tail = ("бэкапы и аварийные алерты по почте перестанут уходить" if gateway
            else "аварийный выход из паузы перестанет работать")
    return f"🗑 Отключить почту?\nЛогин, пароль и серверы будут стёрты; {tail}"


EMAIL_FORGET_CONFIRM = email_forget_confirm()
EMAIL_FORGOTTEN = "✅ Почта отключена"


def email_test_sent(address: str) -> str:
    return f"Письмо ушло на {_e(address)} — проверь входящие"


EMAIL_CHECK_OK = "🟢 Вход по IMAP и SMTP прошёл"


def email_ask_address_change(current: str) -> str:
    return (f"✏️ Смена ящика · сейчас {_e(current)}\n"
            "Пришли адрес нового ящика — после проверки входа он заменит текущий; "
            "до этого старый продолжает работать")


def email_ask_resume_address(current: str) -> str:
    return (f"✉️ Адрес для писем с кодом · сейчас {_e(current)}\n"
            "На него клиент, заперевшийся на паузе, шлёт письмо с кодом. Обычно это сам "
            "ящик; у ящика есть алиас — можно его. Пришли адрес, «-» — сам ящик")


def email_provider_line(address: str, provider) -> str:
    if provider:
        imap, ip, smtp, sp = provider
        return f"Провайдер распознан: IMAP {_e(imap)}:{ip}, SMTP {_e(smtp)}:{sp}"
    return ""


def email_ask_password(address: str) -> str:
    from awgbot.infra import mail
    hint = mail.PASSWORD_HINTS.get(mail.domain_of(address), "")
    tail = f"\n⚠️ {_e(hint)}" if hint else ""
    return ("Пароль ящика — сообщение с ним удалю сразу после приёма; вход по IMAP и SMTP "
            "проверю до сохранения" + tail)


def email_saved(address: str, detail: str) -> str:
    return f"✅ Ящик {_e(address)} подключён\n{_e(detail)}"


def email_check_failed(detail: str) -> str:
    return f"🔴 Не подключено: {_e(detail)}\nНичего не сохранено — попробуй ещё раз"


# ── 🩺 Мониторинг ────────────────────────────────────────────────────────────

def settings_mon_text() -> str:
    from awgbot.core import settings as s
    loud = s.get_bool("app.monitoring.service_failure_alert_loud", True)
    return (f"🩺 Мониторинг · опрос раз в {s.get_int('app.scheduler.monitor_minutes', 3)} мин · "
            f"алерт после {s.get_int('app.monitoring.alert_streak', 5)} "
            f"{plural_ru(s.get_int('app.monitoring.alert_streak', 5), 'плохого замера', 'плохих замеров', 'плохих замеров')} · "
            f"простой AWG дольше {s.get_int('app.monitoring.service_failure_alert_minutes', 5)} мин — "
            + ("со звуком круглые сутки" if loud else "по правилам тихих часов"))


SETTINGS_MON = "🩺 Мониторинг"


# ── 💾 Бэкапы ────────────────────────────────────────────────────────────────

def settings_backup_text(encryption: bool = False, channel: str = "telegram") -> str:
    from awgbot.core import settings as s
    on = s.get_bool("app.scheduler.backup_enabled", True)
    head = "💾 Бэкапы · " + ("✅ вкл" if on else "☑️ выкл") + " · " \
        + ("🔐 фраза задана" if encryption else "🔓 без шифрования")
    lines = [head]
    if on:
        day, hour = s.get_int("app.scheduler.backup_day", 1), s.get_int("app.scheduler.backup_hour", 12)
        where = "на e-mail" if str(channel or "").lower() == "email" else "в этот чат"
        lines.append(f"Каждое {day}-е число в {hour:02d}:00 → {where}")
    lines.append("Восстановить — пришли боту файл бэкапа (.tgz.enc)")
    return "\n".join(lines)


SETTINGS_BACKUP = "💾 Бэкапы\nВосстановить — пришли боту файл бэкапа (.tgz.enc)"


def restore_offer(created_at_iso: str, iface_warning: str = "", gateway: bool = False) -> str:
    """iface_warning — тело предупреждения экрана «Перезапустить AWG» своей
    роли; добавляется, только если восстановление затронет интерфейсы.
    gateway — у агента в копии нет профилей: конфиги линка и туннеля,
    настройки, свои списки."""
    when = timeutil.fmt_dt_ui(timeutil.parse_iso(created_at_iso)) if created_at_iso else "?"
    what = ("конфиги линка и туннеля, настройки, свои списки" if gateway
            else "профили, устройства, подписки, ключи шифрования")
    text = f"♻️ Бэкап от {when} — восстановить?\nВсё вернётся к тому моменту: {what}"
    return text + (f"\n\n{iface_warning}" if iface_warning else "")


def restore_rejected(error: str) -> str:
    return f"⚠️ Файл не принят: {_e(error)}"


RESTORE_STARTED = ("♻️ Восстанавливаю из бэкапа: бот остановится, подменит данные и вернётся "
                   "через несколько секунд — отчёт пришлёт уже новый процесс")


def restore_done(created_at_iso: str) -> str:
    when = timeutil.fmt_dt_ui(timeutil.parse_iso(created_at_iso)) if created_at_iso else "?"
    return f"✅ Восстановлено из бэкапа от {when}"


EMAIL_NOT_CONFIGURED = ("✉️ Почта не настроена — нужен подключённый ящик: ⚙️ Настройки → ✉️ E-mail. "
                        "Настроить сейчас?")
def backup_needs_encryption(gateway: bool = False) -> str:
    keys = "в копии приватные ключи линка и туннеля" if gateway else "в базе приватные ключи устройств"
    return f"По почте уходят только шифрованные копии: {keys}. Задай фразу — 🔐 Шифрование"


BACKUP_NEEDS_ENCRYPTION = backup_needs_encryption()


def backup_encryption_text(mode: str, gateway: bool = False) -> str:
    """Экран «Шифрование»: состояние и правила. gateway — «вне шлюза»."""
    if mode == "passphrase":
        state = "🔐 фраза задана"
    elif mode == "key":
        state = "🔐 случайный ключ (перенесён из env)"
    else:
        state = "🔓 выключено — копии уходят открытыми и по почте не отправляются"
    return (f"🔐 Шифрование бэкапов · {state}\n"
            f"Фразу знаешь только ты — храни вне {'шлюза' if gateway else 'сервера'}, без неё бэкап не открыть"
            + details("Бот принимает фразу сообщением, тут же удаляет и никогда не показывает обратно. "
                      "Смена фразы не перешифровывает старые копии: они открываются прежней — не "
                      "выбрасывай её, пока они нужны"))


BACKUP_ASK_PASSPHRASE = "🔐 Парольная фраза — не короче 8 символов; сообщение удалю сразу после приёма"
BACKUP_ASK_PASSPHRASE_AGAIN = "Повтори фразу ещё раз — так исключим опечатку"
BACKUP_PASSPHRASE_MISMATCH = "⚠️ Фразы не совпали — начнём заново: пришли фразу"
BACKUP_PASSPHRASE_SET = "✅ Фраза задана — следующие копии уйдут шифрованными"
BACKUP_WHEN_PROMPT = "✏️ День и час автобэкапа · сейчас {day}-го в {hour:02d}:00 · пришли два числа: <code>1 12</code>"
BACKUP_WHEN_BAD = "⚠️ Нужны два числа: день месяца 1–28 и час 0–23, например <code>1 12</code>"


def backup_mailed(address: str, n: int = 1) -> str:
    return f"📨 Бэкап отправлен на {_e(address)}"


# ── 🔧 Сервис ────────────────────────────────────────────────────────────────

SVC_CONFIRM_AWG = "🔁 Перезапустить AWG? Все соединения оборвутся на несколько секунд и поднимутся сами"
SVC_CONFIRM_BOT = "🔁 Перезапустить бота? Вернётся через несколько секунд; сервер и соединения не трогаются"
SVC_AWG_RESTARTED = "✅ AWG перезапущен"


def settings_svc_text(state: str, progress=None, available: bool = False) -> str:
    """«🔧 Сервис»: цена перезапусков одной строкой; переезд — только когда идёт."""
    lines = ["🔧 Сервис", "Перезапуск AWG рвёт соединения на несколько секунд, перезапуск бота не влияет на пользователей"]
    if state:
        p = progress
        nums = (f" {p.clients_done}/{p.clients_total} профилей, {p.devices_done}/{p.devices_total} устройств"
                if p is not None and getattr(p, "clients_total", 0) else "")
        lines.append(f"🚚 Переезд идёт:{nums} · выдаются только новые конфиги, отмена безопасна")
    return "\n".join(lines)


SETTINGS_SVC = "🔧 Сервис"


# ── ⬆️ Обновления ────────────────────────────────────────────────────────────

def settings_upd_text(installed: str | None = None, target=None, blocked: str = "",
                      scan_failed: bool = False) -> str:
    """«⬆️ Обновления · v3.1.0 · 🟢 актуальна» или цель обновления со
    списком изменений под «подробнее»; блок — строкой «⛔ … недоступно: …»."""
    from awgbot.core import config
    from .updates import changelog_details
    cur = _ver(installed if installed is not None else config.INSTALLED_VERSION)
    if target is None:
        return f"⬆️ Обновления · {_e(cur)} · " + ("⚪ проверка не удалась" if scan_failed else "🟢 актуальна")
    lines = [f"⬆️ Обновления · {_e(cur)} → {_e(_ver(target.tag))}"]
    if blocked:
        lines.append(f"⛔ Обновление до {_e(_ver(target.tag))} сейчас недоступно: {_e(blocked)}")
    header = "\n".join(lines) + "\n"
    body = changelog_details(getattr(target, "body", "") or "", header=header,   # бюджет — от настоящей шапки
                             tag=str(target.tag))
    if body:
        lines.append(body)
    return "\n".join(lines)


SETTINGS_UPD = "⬆️ Обновления"

# границы валидации ввода: dotted-ключ → (мин, макс, подпись, единица)
SETTINGS_BOUNDS = {
    "quiet_hours.quiet_hours_start": (0, 23, "Тихие часы с", "ч"),
    "quiet_hours.quiet_hours_end": (0, 23, "Тихие часы до", "ч"),
    "resource_alerts.thresholds_percent.cpu": (1, 100, "Порог CPU", "%"),
    "resource_alerts.thresholds_percent.ram": (1, 100, "Порог RAM", "%"),
    "resource_alerts.thresholds_percent.disk": (1, 100, "Порог диска", "%"),
    "limits.traffic_bonus_gb": (1, 100000, "Бонус", "ГБ"),
    "pause.pause_max_total_days": (1, 365, "Дней паузы за год", "дн."),
    "pause.monthly_pause_days": (0, 31, "Дней паузы за месяц", "дн."),
    "grace.grace_days": (1, 365, "Отсрочка", "дн."),
    "app.scheduler.monitor_minutes": (1, 1440, "Частота опроса", "мин"),
    "app.monitoring.alert_streak": (1, 100, "Замеров до алерта", ""),
    "app.monitoring.service_failure_alert_minutes": (1, 1440, "Порог простоя", "мин"),
    "email.poll_interval_sec": (60, 3600, "Опрос почты", "с"),
    "email.resume_code_len": (6, 16, "Длина кода", "символов"),
    "app.scheduler.backup_day": (1, 28, "День автобэкапа", ""),
    "app.scheduler.backup_hour": (0, 23, "Час автобэкапа", "ч"),
    # агент шлюза
    "app.gateway.monitor_minutes": (1, 1440, "Частота опроса", "мин"),
    "app.gateway.handshake_max_age": (1, 1440, "Линк молчит дольше", "мин"),   # хранится в секундах
    "app.gateway.temp_alert_c": (40, 100, "Порог температуры", "°C"),
    # MTU — в новые ссылки. 1280 — минимум IPv6, 1500 — Ethernet без запаса на
    # заголовки туннеля; выше него пакеты начинают фрагментироваться.
    "app.client_config.mtu": (1280, 1500, "MTU", ""),
}


# Текстовые настройки (не числа): ключ → (подпись, подсказка). Ввод проверяется
# валидатором в обработчике.
SETTINGS_TEXT = {
    "email.resume_address": ("Адрес для кода", "адрес почты; «-» — сам ящик"),
    "app.network.server_host": ("Домен", "домен или IP — попадёт в новые ссылки"),
    "app.client_config.server_name": ("Имя сервера", "видно клиенту в приложении"),
    "app.client_config.dns1": ("DNS клиентов", "один или два адреса через запятую"),
    "app.firewall.ssh_allow": ("Адреса для SSH-доступа", "IP, подсеть или имя DynDNS; можно несколько"),
}


def _looks_like_domain(host: str) -> bool:
    import ipaddress
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return True


def _private_dns_note(p: dict) -> str:
    """Хвост строки DNS — чей это резолвер."""
    mode = p.get("mode")
    if mode == "private":
        return " — свой резолвер"
    if mode == "public":
        if p.get("decision") == "pending":
            return " — публичный; при переезде станет свой"
        return " — публичный"
    return ""


PRIVATE_DNS_WHAT = (
    "Сервер умеет отвечать сам — dnsmasq на собственном адресе в туннеле — и это даёт три вещи:\n"
    "• РФ-доступ у всех, а не «через раз»: с публичным адресом Chrome и Edge молча уходят на DoH "
    "мимо сервера, набор адресов не наполняется, и у такого человека российские сайты ругаются;\n"
    "• DNS-запросы клиентов не уходят третьей стороне открытым текстом — резолвит сервер, наружу "
    "идёт уже от него, с кэшем;\n"
    "• защита от DoH-обхода и DNS rebinding с первого дня, а не только со шлюзом.\n"
    "Цена: адрес DNS вшит в каждую выданную ссылку, поэтому переход — это переезд профилей: рядом "
    "поднимается новый интерфейс, у каждого устройства рождается двойник, люди переносят конфиги "
    "когда удобно, старые пиры работают до финала")


def private_dns_offer(target: str) -> str:
    """Экран решения (и инфобокс при старте): что даёт и как перейти."""
    return (f"🔒 Свой DNS-резолвер · сейчас публичный\n"
            f"Свой — {_e(target)}: меньшие задержки, запросы не уходят третьим лицам, защита от "
            "обхода через DoH. Цена — переезд профилей при включении"
            + details(PRIVATE_DNS_WHAT))


PRIVATE_DNS_LATER = ("⏳ Следующий переезд профилей — ручной или при смене поколения ядра — "
                     "перевыпустит конфиги уже со своим резолвером. Отменить правки можно в «🖥 Сервер AWG»")
PRIVATE_DNS_DISMISSED = ("Оставляю публичный DNS. Кнопка «🔒 Свой резолвер» остаётся — передумать "
                         "можно в любой момент")


def settings_server_text(d: dict) -> str:
    """Раздел «Сервер»: четыре строки того, что уезжает в новые ссылки, и
    «подробнее» про переезд."""
    host = d.get("host") or ""
    kernel = _e(d.get("kernel") or "не определено")
    gen = f", gen{d['generation']}" if d.get("generation") else ""
    dns_note = _private_dns_note(d.get("private_dns") or {})
    lines = [
        f"🖥 Сервер AWG · ядро {kernel}{gen}",
        (f"{_e(host)} · имя сервера: «{_e(d['name'])}»" if _looks_like_domain(host)
         else f"Домена нет, в ссылках IP {_e(host)} · имя сервера: «{_e(d['name'])}»"),
        f"DNS {_e(d['dns'])}{dns_note} · MTU {d['mtu']} · keepalive {_e(str(d['keepalive']))}",
        f"{_e(d['iface'])} · порт {d['port']} · {_e(d['subnet'])}",
    ]
    warns = []
    conf_port = d.get("port_conf")
    if conf_port and d.get("port") and int(conf_port) != int(d["port"]):
        warns.append(f"⚠️ В конфиге бота порт {conf_port}, а интерфейс слушает {d['port']} — "
                     "ссылки идут с портом интерфейса, проверки смотрят в конфиг; поправь app.yaml")
    blocked = d.get("migration_blocked") or ""
    if blocked:
        warns.append(f"🚚 Сменить порт или подсеть сейчас нельзя: {_e(blocked)}")
    lines += warns
    lines.append(details("Правки уходят только в новые ссылки: старые несут те параметры, с которыми "
                         "их выдали. Порт и подсеть меняются переездом: у устройств появляются двойники, "
                         "пользователи перевыпускают конфиги, когда им удобно"))
    return "\n".join(lines)


_ALLOW_SHOWN = 12       # кнопки — до 8, текст — до 12: лимит 4096 при длинных именах


def _shown(items, code: bool = True) -> str:
    out = ", ".join((f"<code>{_e(a)}</code>" if code else _e(a)) for a in items[:_ALLOW_SHOWN])
    if len(items) > _ALLOW_SHOWN:
        out += f" и ещё {len(items) - _ALLOW_SHOWN}"
    return out


def address_list_line(n: int, tail: str = "") -> str:
    """Список адресов в инфобокс не выносится — он редактируется кнопками
    под ним; здесь только число и отсылка."""
    if not n:
        return "Адреса для SSH-доступа (фильтр): не заданы" + tail
    return (f"Адреса для SSH-доступа (фильтр): {n} "
            + plural_ru(n, "адрес", "адреса", "адресов") + " — редактируемый список ниже")


def warnings_block(items: list[str]) -> list[str]:
    """Предупреждения раздела — отдельным блоком после пустой строки, по
    одному на строку; нет предупреждений — ничего."""
    return ["", "<b>Предупреждения:</b>", *items] if items else []


def settings_firewall_text(st: dict) -> str:
    """Раздел «SSH-доступ»: порт, туннель, снаружи, адреса, предупреждения.
    Текст утверждён на вычитке 3.1.0 — только заголовок по новому имени."""
    allow = st.get("raw_allow") or []
    port_line = f"Порт SSH: {st.get('ssh_port')}"
    if st.get("owner") == "omv":
        port_line += " — <b>контролирует OMV</b> <i>(в его UI: Службы → SSH)</i>"
    elif st.get("owner"):
        port_line += " — <b>контролирует другой процесс</b>"
    lines = ["<b>🛡 SSH-доступ</b>", "", port_line, ""]
    if st.get("admin_ips"):
        lines += [f"Из туннеля SSH открыт устройствам админа ({len(st['admin_ips'])})", ""]
    if st.get("enabled"):
        lines.append("🟢 Снаружи: фильтр включён — только адреса из списка")
    else:
        lines.append("Снаружи: фильтр выключен — открыт всем (только по SSH-ключам)")
    lines.append("")
    lines.append(address_list_line(len(allow)))
    warns: list[str] = []
    if st.get("unresolved"):
        warns.append("⚠️ Не резолвятся: " + _shown(st["unresolved"], code=False))
    if st.get("drift"):
        warns.append(f"⚠️ sshd слушает порт {st['listening']}, а фильтр держит {st.get('ssh_port')} — "
                     f"вход снаружи и из туннеля закрыт. Нажми «🅿️ Порт» → {st['listening']} "
                     f"или верни sshd на {st.get('ssh_port')}")
    elif st.get("sshd_down"):
        warns.append("⚪ sshd не запущен")
    if st.get("owner") == "generator" and st.get("owner_detail"):
        warns.append(f"ℹ️ В <code>{_e((st.get('owner_files') or ['sshd_config'])[0])}</code> сказано: "
                     f"«<i>{_e(st['owner_detail'])}</i>»")
    if st.get("ufw"):
        warns.append("⚠️ ufw активен: второй владелец правил, лучше выключить (<code>ufw disable</code>)")
    if st.get("firewalld"):
        warns.append("⚠️ firewalld активен: второй владелец правил, новый порт открывай и в нём или выключи его")
    return "\n".join(lines + warnings_block(warns))


def ssh_owner_refusal(st: dict, listening: int | None, place: str = "сервере") -> str:
    """Отказ смены порта: конфигом sshd владеет не бот. Экран, не alert —
    текст длинный и нужен целиком. place — «сервере» / «шлюзе»."""
    if st.get("owner") == "omv":
        now = f"sshd слушает {listening}" if listening else "sshd не запущен"
        if st.get("owner_port"):
            now += f", в OMV задан {st['owner_port']}"
        return (f"⛔ <b>Смена порта SSH не выполнена: файлом sshd_config на этом {place} управляет OMV.</b>\n\n"
                "Порт задаётся в OMV: Службы → SSH → «Порт», затем «Применить» в жёлтой плашке. "
                "Если поменять его здесь, настройка проживёт до первого применения изменений в OMV — "
                "он перепишет sshd_config своим шаблоном, и sshd вернётся на порт из OMV.\n\n"
                "Что сделает бот сам: увидит новый порт (сверяет каждые несколько минут) и переведёт "
                "на него фильтр — из туннеля, из локальной сети и снаружи. Проброс порта на роутере "
                f"(при наличии) поправь сам.\n\nСейчас: {now}.")
    f = (st.get("owner_files") or ["/etc/ssh/sshd_config"])[0]
    return (f"⛔ <b>Смена порта SSH не выполнена: файлом sshd_config на этом {place} управляет другой "
            f"процесс.</b>\n\nВ <code>{_e(f)}</code> сказано: «<i>{_e(st.get('owner_detail') or '')}</i>». "
            "Порт меняй там, откуда файл генерируется, иначе настройка проживёт до его следующей "
            "генерации. Бот увидит новый порт сам и переведёт на него фильтр.")


def ssh_port_ask(current: int | None = None, gateway: bool = False) -> str:
    cur = f" · сейчас {current}" if current else ""
    tail = ". Проброс порта на роутере (при наличии) поправь сам" if gateway else ""
    return (f"🅿️ Порт SSH{cur} · 1–65535. Занятый порт не возьму; текущие сеансы не рвутся — "
            f"проверь вход новым подключением{tail}")


SSH_PORT_ASK = ssh_port_ask()


def ssh_port_busy(port: int, proc: str = "") -> str:
    who = f" уже занят процессом <code>{_e(proc)}</code>" if proc else " занят"
    return f"⛔ Смена порта SSH не выполнена: порт {port}{who}"


def ssh_port_same(port: int) -> str:
    return f"ℹ️ Порт SSH не изменился — выбран уже установленный ({port})"


def ssh_port_changed(old: int, new: int) -> str:
    return (f"✅ Порт SSH: {old} → {new}. Текущие сеансы не рвутся — проверь вход новым "
            f"подключением на порт {new}; файервол провайдера, ufw или fail2ban — открой порт и там")


def firewall_confirmed() -> str:
    return "✅ Таймер снят, фильтр остаётся включённым"


def firewall_rolled_back() -> str:
    return "↩️ Правила сняты, SSH снова открыт всем адресам. NAT клиентов на месте"


FIREWALL_ON_ALERT = ("Фильтр включён: снаружи — только адреса из списка, из туннеля — устройства "
                     "админа. Проверь вход новым подключением")


def settings_prompt(key: str, current=None) -> str:
    """«✏️ Частота опроса · сейчас 3 мин · 1–1440» — приглашение к вводу с
    текущим значением и границами; текстовые — с подсказкой."""
    if key in SETTINGS_TEXT:
        label, hint = SETTINGS_TEXT[key]
        if isinstance(current, (list, tuple)):
            shown = ", ".join(str(x) for x in list(current)[:5]) + (f" и ещё {len(current) - 5}" if len(current) > 5 else "")
        else:
            shown = str(current) if current not in (None, "", []) else ""
        cur = f" · сейчас {_e(shown)}" if shown else ""
        icon = "➕" if key == "app.firewall.ssh_allow" else "✏️"      # эмодзи кнопки «➕ Адрес»
        return f"{icon} {_e(label)}{cur}\n{_e(hint)}"
    lo, hi, label, unit = SETTINGS_BOUNDS[key]
    u = unit_suffix(unit)
    cur = f" · сейчас {current}{u}" if current is not None else ""
    return f"✏️ {_e(label)}{cur} · {lo}–{hi}"


def unit_suffix(unit: str) -> str:
    """Единица после числа: «%» вплотную, остальные через пробел."""
    if not unit:
        return ""
    return unit if unit == "%" else f" {unit}"


def settings_changed(key: str, old, new) -> str:
    """Итог ввода — первой строкой раздела: «✅ Частота опроса: 3 → 5 мин»."""
    if key in SETTINGS_TEXT:
        label, unit = SETTINGS_TEXT[key][0], ""
    else:
        _lo, _hi, label, unit = SETTINGS_BOUNDS[key]
        unit = unit_suffix(unit)
    old_s = _e(str(old)) if old not in (None, "", []) else "—"
    return f"✅ {_e(label)}: {old_s} → {_e(str(new))}{unit}"


def settings_ssh_allow_added(entries: list) -> str:
    # адреса — моноширинным: жирный адрес Telegram превращает в ссылку
    return ("✅ Адреса для SSH-доступа: добавлено "
            + ", ".join(f"<code>{_e(x)}</code>" for x in entries))


def settings_bad_value(key: str) -> str:
    lo, hi, _label, unit = SETTINGS_BOUNDS[key]
    return f"⚠️ Нужно целое число {lo}–{hi}{unit_suffix(unit)}"


def cycle_toast(key: str, value) -> str:
    """Всплывашка после кнопки-цикла: «Опрос: 5 мин», «Код: 12 символов»,
    «Проверка: неделя», «Куда: E-mail»."""
    if key == "email.poll_interval_sec":
        return f"Опрос: {int(value) // 60} мин"
    if key == "email.resume_code_len":
        return f"Код: {value} символов"
    if key == "updates.poll_schedule":
        from awgbot.bot.keyboards.settings import UPDATE_SCHEDULE_LABELS
        return f"Проверка: {UPDATE_SCHEDULE_LABELS.get(str(value), value)}"
    if key == "app.scheduler.backup_channel":
        return "Куда: " + ("E-mail" if str(value) == "email" else "Telegram")
    if key == "app.routing.probe_seconds":
        return f"Такт: {value} с"
    if key == "app.routing.failover.window_samples":
        return f"Окно: {value}"
    if key == "app.routing.failover.min_availability":
        return f"Порог: {value}%"
    if key == "app.routing.lists_refresh_hours":
        return f"Списки: раз в {value} ч"
    return "Готово"
