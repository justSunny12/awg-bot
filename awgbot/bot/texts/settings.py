"""Экран настроек: разделы, почта, резервные копии, сервер, файервол, границы значений, финишеры."""

from __future__ import annotations

from .fmt import _e, _fmt_age
from .migration import migration_panel_line
from .updates import _ver


# ── Экран настроек ───────────────────────────────────────────────────────────
SETTINGS_ROOT = "⚙️ <b>Настройки</b>\n\nВыбери раздел. Изменения применяются сразу."
SETTINGS_NOTIFY_CLIENTS = ("👥 <b>События профилей</b>\n\nКакие события профилей "
                           "присылать админу.")
SETTINGS_NOTIFY = ("🔔 <b>Уведомления</b>\n\nЗапасной канал, тихие часы (ночью без звука), "
                   "алерты о загрузке хоста и уведомления о событиях профилей.\n\n"
                   "«E-mail при недоступности Telegram»: если Telegram не отвечает, на ящик "
                   "бота уходят <b>только критичные</b> алерты — те, что бьют по всем клиентам: "
                   "падение VPN-сервиса, недоступность шлюза маршрутизации, перегруз хоста. "
                   "Остальные уведомления по почте не дублируются.")
SETTINGS_SUBS = (
    "💳 <b>Параметры подписок</b>\n\n"
    "Правила, общие для всех профилей. Действуют с момента правки и на уже "
    "выданные подписки тоже.\n\n"
    "• <b>Бонус-квота</b> — сколько трафика добавляется профилю по кнопке "
    "«добавить квоту», когда он упёрся в лимит и просит ещё.\n"
    "• <b>Пауза</b> останавливает срок подписки: неиспользованные дни "
    "возвращаются в конце.\n"
    "• <b>Дней паузы (год)</b> — сколько дней паузы начислять профилям с "
    "<b>годовой</b> подпиской при продлении (даже несвоевременном).\n"
    "<i>Максимум накопления: за два продления.</i>\n"
    "• <b>Дней паузы (месяц)</b> — сколько дней паузы начислять профилям с "
    "<b>ежемесячной</b> подпиской за своевременное продление. Переход подписки "
    "в статус «истекла» или использование грейс-периода исключают бонус за "
    "следующее продление на месяц.\n"
    "<i>Максимум накопления: за 12 своевременных продлений.</i>\n"
    "• <b>Грейс-период</b> — сколько дней после окончания подписки профиль ещё "
    "работает. Нужен, чтобы истёкший в отпуске или ночью не отваливался молча: "
    "бот предупреждает, а доступ пока живёт.\n\n"
    "Лимиты конкретного профиля (устройства, трафик, срок) — в его карточке.")


def settings_email_text(acc, last_check: tuple, resume_on=None, resume_addr: str = "") -> str:
    """Экран почтового канала: ящик, серверы, состояние, функции на канале.
    resume_on=None — у агента шлюза аварийного выхода нет, блок не рисуется."""
    from awgbot.util import timeutil
    lines = ["✉️ <b>E-mail</b>", ""]
    if acc is None:
        what = ("для аварийного выхода из приостановки по коду в письме, бэкапов и "
                "критичных алертов" if resume_on is not None
                else "для бэкапов и критичных алертов, когда Telegram недоступен; "
                     "настройки почты приезжают в конфигурации шлюза с ВПС")
        lines.append(f"Ящик не подключён. Почта нужна {what}; портов на хосте не "
                     "открывается — бот сам ходит на почтовый сервер.")
        return "\n".join(lines)
    lines.append(f"Ящик: <code>{_e(acc.login)}</code>")
    lines.append(f"IMAP: <code>{_e(acc.imap_host)}:{acc.imap_port}</code>, "
                 f"SMTP: <code>{_e(acc.smtp_host)}:{acc.smtp_port}</code>")
    state, iso, detail = last_check
    if state == "ok":
        when = _fmt_age((timeutil.now() - timeutil.parse_iso(iso)).total_seconds()) if iso else ""
        lines.append(f"Состояние: ✅ проверено {when}".rstrip())
    elif state == "fail":
        lines.append(f"Состояние: 🔴 {_e(detail)}")
    else:
        lines.append("Состояние: ⚪ ещё не проверялось")
    if resume_on is not None:
        lines += ["", "🆘 Аварийный выход из приостановки: " + ("включён" if resume_on else "выключен")]
        if resume_on:
            lines.append(f"Адрес для писем с кодом: <code>{_e(resume_addr)}</code>")
    return "\n".join(lines)


EMAIL_ASK_ADDRESS = ("✉️ <b>Подключение ящика</b>\n\nПришли адрес ящика, от имени которого "
                     "бот будет читать и слать почту, например <code>box@icloud.com</code>.")
EMAIL_ASK_IMAP_HOST = "Домен незнакомый — укажи серверы руками.\n\nIMAP-сервер (приём):"
EMAIL_ASK_IMAP_PORT = "Порт IMAP (SSL/TLS), обычно 993:"
EMAIL_ASK_SMTP_HOST = "SMTP-сервер (отправка):"
EMAIL_ASK_SMTP_PORT = "Порт SMTP (STARTTLS), обычно 587:"
EMAIL_BAD_ADDRESS = "⚠️ Это не похоже на адрес почты. Пришли адрес вида box@example.com."
EMAIL_BAD_PORT = "⚠️ Нужен номер порта от 1 до 65535."
EMAIL_BAD_HOST = "⚠️ Нужно имя сервера, например imap.example.com."
EMAIL_FORGET_CONFIRM = ("🗑 <b>Отключить почту?</b>\n\nЛогин, пароль и серверы будут стёрты. "
                        "Аварийный выход из приостановки перестанет работать.")
EMAIL_FORGOTTEN = "✅ Почта отключена."
def email_test_sent(address: str) -> str:
    return f"📨 Тестовое письмо отправлено на ящик <code>{_e(address)}</code> — проверь входящие."


def email_ask_address_change(current: str) -> str:
    return (f"✏️ <b>Смена ящика</b>\n\nСейчас подключён <code>{_e(current)}</code>. Пришли адрес "
            f"нового ящика — после проверки входа он заменит текущий; пока проверка не "
            f"пройдена, старый продолжает работать.")


def email_ask_resume_address(current: str) -> str:
    return (f"✉️ <b>Адрес для писем с кодом</b>\n\nНа этот адрес клиент, заперевшийся в "
            f"приостановке, шлёт письмо с кодом аварийного выхода. Обычно это сам ящик; "
            f"если у ящика есть алиас, можно указать его — бот читает один и тот же ящик.\n\n"
            f"Сейчас: <code>{_e(current)}</code>\n\nПришли новый адрес, или «-», чтобы вернуть сам ящик.")


def email_provider_line(address: str, provider) -> str:
    if provider:
        imap, ip, smtp, sp = provider
        return f"Провайдер распознан: IMAP {_e(imap)}:{ip}, SMTP {_e(smtp)}:{sp}."
    return ""


def email_ask_password(address: str) -> str:
    from awgbot.infra import mail
    hint = mail.PASSWORD_HINTS.get(mail.domain_of(address), "")
    tail = f"\n\n⚠️ {_e(hint)}." if hint else ""
    return ("Пароль ящика. Сообщение с ним бот удалит сразу после приёма; проверка входа "
            "по IMAP и SMTP пройдёт до сохранения." + tail)


def email_saved(address: str, detail: str) -> str:
    return f"✅ Ящик <code>{_e(address)}</code> подключён.\n{_e(detail)}"


def email_check_failed(detail: str) -> str:
    return f"🔴 Не подключено: {_e(detail)}\n\nНичего не сохранено. Попробуй ещё раз."
SETTINGS_MON = ("📊 <b>Мониторинг</b>\n\nЧастота опроса, чувствительность алертов "
                "и поведение при простое AWG.")
SETTINGS_BACKUP = ("💾 <b>Резервное копирование</b>\n\nРасписание автоматического "
                  "резервного копирования и ручной запуск. Доставка — в этот чат или на "
                  "e-mail (доступно при включенном шифровании).\n\n"
                  "<b>Для восстановления из резервной копии отправь боту файл от нужной "
                  "даты (формат файла - tgz.enc)</b>")


def restore_offer(created_at_iso: str, iface_warning: str = "") -> str:
    """iface_warning — тело предупреждения экрана «Перезапустить AWG» своей
    роли; добавляется, только если восстановление затронет интерфейсы."""
    from awgbot.util import timeutil
    when = timeutil.fmt_dt(timeutil.parse_iso(created_at_iso)) if created_at_iso else "?"
    text = (f"Приложенный тобой файл - бэкап настроек бота и сервиса от {when}.\n"
            "Восстановить из него?\n"
            "<b>Важно!</b> Все изменения, внесенные в настройки бота и сервиса (в т.ч. "
            "добавленные профили и устройства, измененные подписки, ключи шифрования) будут "
            "возвращены к состоянию на момент снятия резервной копии!")
    return text + (f"\n\n{iface_warning}" if iface_warning else "")


def restore_rejected(error: str) -> str:
    return f"⚠️ Файл не принят: {_e(error)}."


RESTORE_STARTED = ("♻️ Восстанавливаю из резервной копии. Бот остановится, подменит данные и "
                   "вернётся через несколько секунд — отчёт пришлёт уже новый процесс.")


def restore_done(created_at_iso: str) -> str:
    from awgbot.util import timeutil
    when = timeutil.fmt_dt(timeutil.parse_iso(created_at_iso)) if created_at_iso else "?"
    return f"✅ Восстановлено из резервной копии от {when}."
EMAIL_NOT_CONFIGURED = ("✉️ <b>Почта не настроена</b>\n\nДля этого нужен подключённый ящик: "
                        "⚙️ Настройки → ✉️ E-mail. Настроить сейчас?")
BACKUP_NEEDS_ENCRYPTION = ("⚠️ По почте уходят только шифрованные копии: в БД приватные ключи "
                           "устройств. Задай парольную фразу: 🔐 Шифрование.")


def backup_encryption_text(mode: str) -> str:
    """Экран «Шифрование»: состояние и правила."""
    if mode == "passphrase":
        state = "✅ парольная фраза задана"
    elif mode == "key":
        state = "✅ случайный ключ (перенесён из env)"
    else:
        state = "🔴 выключено — копии уходят открытыми, по почте не отправляются"
    return ("🔐 <b>Шифрование резервных копий</b>\n\n"
            f"Состояние: {state}\n\n"
            "Копии шифруются парольной фразой, которую знаешь только ты: бот принимает "
            "её сообщением, тут же удаляет и никогда не показывает обратно. Держи фразу "
            "вне этого хоста — без неё копию не восстановить.\n\n"
            "Смена фразы не перешифровывает старые копии: они открываются прежней фразой, "
            "не выбрасывай её, пока они нужны.")


BACKUP_ASK_PASSPHRASE = ("Пришли парольную фразу (не короче 8 символов). Сообщение будет "
                         "удалено сразу после приёма.")
BACKUP_ASK_PASSPHRASE_AGAIN = "Повтори фразу ещё раз — так исключим опечатку."
BACKUP_PASSPHRASE_MISMATCH = "⚠️ Фразы не совпали. Начнём заново: пришли фразу."
BACKUP_PASSPHRASE_SET = "✅ Парольная фраза задана. Следующие копии уйдут шифрованными."


def backup_mailed(address: str, n: int = 1) -> str:
    return f"📨 Резервная копия отправлена на ящик <code>{_e(address)}</code>"
SETTINGS_SVC = ("🔄 <b>Обслуживание</b>\n\n"
                "• <b>Мониторинг</b> — как часто бот опрашивает сервер и когда "
                "считать простой аварией;\n"
                "• <b>Резервное копирование</b> — расписание, шифрование и куда "
                "уходят копии;\n"
                "• <b>Перезапуск AWG</b> — коннекты оборвутся на секунды и "
                "поднимутся сами;\n"
                "• <b>Перезапуск бота</b> — сервер и коннекты не трогаются.")
SVC_CONFIRM_AWG = ("🔄 <b>Перезапустить AWG?</b>\n\nСервер AmneziaWG перезапустится: все "
                   "коннекты оборвутся на несколько секунд и поднимутся сами.")
SVC_CONFIRM_BOT = ("🔄 <b>Перезапустить бота?</b>\n\nБот перезапустится и вернётся через "
                   "несколько секунд. Сервер AmneziaWG и коннекты не трогаются.")


def settings_svc_text(state: str, progress=None, available: bool = False) -> str:
    """Обслуживание. Про переезд рассказываем ровно тогда, когда его кнопка
    здесь есть: без кнопки это разговор о механизме, которого человек в этом
    экране не видит."""
    if not state:
        if not available:
            return SETTINGS_SVC
        return SETTINGS_SVC + ("\n\n🚚 <b>Переезд профилей</b> — смена параметров "
                               "интерфейса без флаг-дня: рядом со старым работает "
                               "новый, у каждого устройства рождается двойник, люди "
                               "переносят конфиги когда удобно.")
    body = SETTINGS_SVC + "\n\n🚚 <b>Переезд профилей идёт.</b>\n"
    line = migration_panel_line(progress)
    if line:
        body += line.replace("🚚 Процесс миграции: ", "Готовность: ") + "\n"
    return body + ("Выдаются только новые конфиги. Отмена безопасна в любой "
                   "момент: коннекты переехавших не рвутся.")
SETTINGS_UPD = ("⬆️ <b>Обновления бота</b>\n\nАвтоуведомления, периодичность проверки "
                "и ручная проверка новой версии.")


def settings_upd_text(installed: str | None = None) -> str:
    """Раздел обновлений основного бота — с текущей версией последней строкой:
    до этого её было не увидеть, не запросив проверку."""
    from awgbot.core import config
    cur = _ver(installed if installed is not None else config.INSTALLED_VERSION)
    return SETTINGS_UPD + f"\n\nТекущая версия бота: <b>{_e(cur)}</b>"

# границы валидации ввода: dotted-ключ → (мин, макс, подпись, единица)
SETTINGS_BOUNDS = {
    "quiet_hours.quiet_hours_start": (0, 23, "Начало тихих часов", "час (0–23)"),
    "quiet_hours.quiet_hours_end": (0, 23, "Конец тихих часов", "час (0–23)"),
    "resource_alerts.thresholds_percent.cpu": (1, 100, "Порог CPU", "% (1–100)"),
    "resource_alerts.thresholds_percent.ram": (1, 100, "Порог RAM", "% (1–100)"),
    "resource_alerts.thresholds_percent.disk": (1, 100, "Порог диска", "% (1–100)"),
    "limits.traffic_bonus_gb": (1, 100000, "Бонус-квота", "ГБ"),
    "pause.pause_max_total_days": (1, 365, "Дней паузы (год)", "дней (1–365)"),
    "pause.monthly_pause_days": (0, 31, "Дней паузы (месяц)", "дней (0–31)"),
    "grace.grace_days": (1, 365, "Grace-дней", "дней (1–365)"),
    "app.scheduler.monitor_minutes": (1, 1440, "Частота опроса", "мин (1–1440)"),
    "app.monitoring.alert_streak": (1, 100, "Порог стрика", "замеров (1–100)"),
    "app.monitoring.service_failure_alert_minutes": (1, 1440, "Порог простоя", "мин (1–1440)"),
    "email.poll_interval_sec": (60, 3600, "Интервал опроса почты", "сек (60–3600)"),
    "email.resume_code_len": (6, 16, "Длина кода", "символов (6–16)"),
    "app.scheduler.backup_day": (1, 28, "День автобэкапа", "число месяца (1–28)"),
    "app.scheduler.backup_hour": (0, 23, "Час автобэкапа", "час (0–23)"),
    # агент шлюза
    "app.gateway.monitor_minutes": (1, 1440, "Частота опроса", "мин (1–1440)"),
    "app.gateway.handshake_max_age": (60, 86400, "Порог простоя линка", "сек (60–86400)"),
    "app.gateway.temp_alert_c": (40, 100, "Порог температуры", "°C (40–100)"),
    # MTU — в новые ссылки. 1280 — минимум IPv6, 1500 — Ethernet без запаса на
    # заголовки туннеля; выше него пакеты начинают фрагментироваться.
    "app.client_config.mtu": (1280, 1500, "MTU клиентов", "байт (1280–1500)"),
}


# Текстовые настройки (не числа): ключ → (подпись, подсказка). Ввод проверяется
# валидатором в обработчике.
SETTINGS_TEXT = {
    "email.resume_address": ("Адрес для писем с кодом", "адрес почты; пусто — сам ящик"),
    "app.network.server_host": ("Доменное имя", "домен или IP — попадёт в НОВЫЕ ссылки"),
    "app.client_config.server_name": ("Имя сервера", "видно клиенту в приложении"),
    "app.client_config.dns1": ("DNS клиентов", "один или два адреса через запятую"),
    "app.firewall.ssh_allow": ("Адреса для SSH", "IP, подсеть или имя DynDNS; можно несколько"),
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
    """Хвост строки «DNS клиентов» — чей это резолвер."""
    mode = p.get("mode")
    if mode == "private":
        return " — свой резолвер на сервере"
    if mode == "public":
        if p.get("decision") == "pending":
            return " — публичный; при следующем переезде станет свой"
        return " — публичный"
    return ""


PRIVATE_DNS_WHAT = (
    "🔒 <b>Свой DNS-резолвер для клиентов</b>\n\n"
    "Сейчас в конфигах клиентов публичный DNS. Сервер умеет отвечать сам — "
    "dnsmasq на собственном адресе в туннеле — и это даёт три вещи:\n"
    "• <b>условная маршрутизация у всех, а не «через раз»</b>: с публичным "
    "адресом Chrome и Edge молча уходят на DoH мимо сервера, набор адресов "
    "не наполняется, и у такого человека российские сайты ругаются на адрес;\n"
    "• DNS-запросы клиентов не уходят третьей стороне открытым текстом — "
    "резолвит сервер, наружу идёт уже от него, с кэшем;\n"
    "• защита от DoH-обхода и DNS rebinding с первого дня, а не только со шлюзом.\n\n"
    "Цена: адрес DNS вшит в каждую выданную ссылку, поэтому переход — это "
    "переезд профилей: рядом поднимается новый интерфейс, у каждого устройства "
    "рождается двойник, люди переносят конфиги когда удобно, старые пиры "
    "работают до финала.")


def private_dns_offer(target: str) -> str:
    """Экран решения (и инфобокс при старте): что даёт и как перейти."""
    return (PRIVATE_DNS_WHAT + "\n\nАдрес резолвера будет "
            f"<code>{_e(target)}</code> (новая подсеть — свой .1).\n\n"
            "Переехать сейчас, при следующем переезде по любому поводу — или не нужно.")


PRIVATE_DNS_LATER = ("⏳ Хорошо: следующий переезд профилей — ручной или при смене "
                     "поколения ядра — перевыпустит конфиги уже со своим резолвером. "
                     "Передумать можно в «🖥 Сервер AWG».")
PRIVATE_DNS_DISMISSED = ("Хорошо, оставляю публичный DNS. Кнопка «🔒 Свой DNS-резолвер» "
                         "в «🖥 Сервер AWG» остаётся — передумать можно в любой момент.")


def settings_server_text(d: dict) -> str:
    """Раздел «Сервер»: что уезжает в новые ссылки и что менять нельзя."""
    host = d.get("host") or ""
    lines = [
        "<b>🖥 Сервер AWG</b>", "",
        # Доменного имени может не быть вовсе: в ссылки тогда уезжает IP,
        # определённый при установке. «Пусто» в этой строке — не ошибка.
        f"Доменное имя: <code>{_e(host)}</code>" if _looks_like_domain(host)
        else f"Доменное имя: не задано (в ссылках IP <code>{_e(host)}</code>)",
        f"Имя сервера: {_e(d['name'])}",
        f"DNS клиентов: <code>{_e(d['dns'])}</code>" + _private_dns_note(d.get("private_dns") or {}),
        f"MTU: {d['mtu']} · keepalive: {_e(str(d['keepalive']))}",
        "",
        f"Интерфейс: <code>{_e(d['iface'])}</code> · порт {d['port']} · "
        f"подсеть <code>{_e(d['subnet'])}</code>",
        f"Ядро AmneziaWG: {_e(d['kernel'] or 'не определено')}"
        + (f" · поколение {d['generation']}" if d.get("generation") else ""),
        "",
        "Правки применяются к <b>новым</b> ссылкам: уже выданные конфиги несут то, "
        "с чем их выдали.",
    ]
    blocked = d.get("migration_blocked") or ""
    if blocked:
        lines += ["", f"🚚 Сменить порт или подсеть сейчас нельзя: {_e(blocked)}."]
    else:
        lines += ["", "Порт и подсеть меняются переездом профилей: у каждого "
                      "устройства рождается двойник на новых параметрах, люди "
                      "переносят конфиги когда удобно, старые пиры всё это время "
                      "работают."]
    conf_port = d.get("port_conf")
    if conf_port and d.get("port") and int(conf_port) != int(d["port"]):
        lines.insert(-1, f"⚠️ В конфиге бота порт {conf_port}, а интерфейс слушает "
                         f"{d['port']} — бот раздаёт ссылки с портом интерфейса, "
                         f"но проверки смотрят в конфиг. Поправь app.yaml.")
    return "\n".join(lines)


def settings_firewall_text(st: dict) -> str:
    """Раздел «Файервол»: состояние и что будет при включении."""
    if st.get("rollback"):
        return ("<b>🛡 Доступ по SSH</b>\n\n⏱ <b>Идёт проверка входа.</b>\n\n"
                "Правила уже применены. Открой <b>новое</b> SSH-подключение к серверу "
                "и, если оно проходит, подтверди здесь. Не подтвердишь — правила "
                "снимутся сами, доступ вернётся всем адресам.\n\n"
                "Это и есть страховка: заперев себе SSH, ты не сможешь ничего "
                "исправить в терминале, зато этот чат работает независимо.")
    head = "🟢 фильтр включён" if st.get("enabled") else "🔴 фильтр выключен"
    allow = st.get("raw_allow") or []
    lines = [
        "<b>🛡 Доступ по SSH</b>", "",
        f"{head} · порт SSH {st.get('ssh_port')}",
        ("Адреса для входа снаружи: " + ", ".join(f"<code>{_e(a)}</code>" for a in allow))
        if allow else "Адреса не заданы — SSH открыт всем (только по ключам).",
    ]
    if st.get("unresolved"):
        lines.append("⚠️ не резолвятся: " + ", ".join(_e(x) for x in st["unresolved"]))
    if st.get("admin_ips"):
        lines.append(f"Из туннеля SSH открыт устройствам админа ({len(st['admin_ips'])}) — всегда.")
    if st.get("ufw"):
        lines.append("⚠️ ufw активен: второй владелец правил, лучше выключить (<code>ufw disable</code>).")
    if not st.get("enabled"):
        # Про таймер говорим ровно там, где его вот-вот поставят: на включённом
        # фильтре это уже прошедшее время, и строка только занимает место.
        lines += ["", "Включение применяет правила с таймером: если вход по SSH "
                      "сломается, они снимутся сами."]
    return "\n".join(lines)


SSH_PORT_ASK = ("🅿️ <b>Порт SSH</b>\n\nПришли номер порта (1–65535). Занятый порт "
                "не возьму. Переведу на него sshd и фильтр; текущие SSH-сеансы "
                "не рвутся — проверь вход новым подключением.")


def ssh_port_busy(port: int) -> str:
    return f"⛔ Порт {port} занят, необходимо выбрать другой."


def ssh_port_same(port: int) -> str:
    return (f"ℹ️ Порт доступа по SSH не изменился, т.к. выбран ранее уже "
            f"установленный ({port}).")


def ssh_port_changed(old: int, new: int) -> str:
    return (f"✅ Порт SSH изменён: {old} → <b>{new}</b>. Проверь вход <b>новым</b> "
            f"подключением на порт {new}; текущие сеансы живут. Если снаружи стоит "
            "файервол провайдера — открой в нём новый порт.")


def firewall_armed(seconds: int) -> str:
    return (f"⏱ Правила применены. Проверь вход <b>новым</b> SSH-подключением и "
            f"подтверди в течение {seconds // 60} мин — иначе они снимутся сами.")


def firewall_confirmed() -> str:
    return "✅ Таймер снят, фильтр остаётся включённым."


def firewall_rolled_back() -> str:
    return ("↩️ Правила сняты, SSH снова открыт всем адресам. "
            "NAT клиентов на месте.")


def settings_prompt(key: str) -> str:
    if key in SETTINGS_TEXT:
        label, hint = SETTINGS_TEXT[key]
        return f"Введи новое значение: <b>{_e(label)}</b>\n{_e(hint)}."
    lo, hi, label, unit = SETTINGS_BOUNDS[key]
    return f"Введи новое значение: <b>{_e(label)}</b>\nЕдиница: {_e(unit)}\nДиапазон: {lo}–{hi}."


# Род подписи настройки — для согласованного глагола в финишере («Частота
# опроса изменена», «Порог CPU изменён», «Доменное имя изменено»). Не в списке
# — мужской.
_SETTINGS_GENDER = {
    "quiet_hours.quiet_hours_start": "n",            # Начало
    "limits.traffic_bonus_gb": "f",                  # Бонус-квота
    "grace.grace_days": "n",                         # Grace-дней (количество)
    "pause.pause_max_total_days": "n",               # Дней паузы (год) (количество)
    "pause.monthly_pause_days": "n",                 # Дней паузы (месяц) (количество)
    "app.scheduler.monitor_minutes": "f",            # Частота
    "app.gateway.monitor_minutes": "f",
    "email.resume_code_len": "f",                    # Длина
    "app.network.server_host": "n",                  # Доменное имя
    "app.client_config.server_name": "n",            # Имя сервера
    "app.firewall.ssh_allow": "pl",                  # Адреса
}
_CHANGED = {"m": "изменён", "f": "изменена", "n": "изменено", "pl": "изменены"}


def settings_changed(key: str, old, new) -> str:
    """Финишер после ввода значения — остаётся в чате, раздел приходит следом.
    Единица — из таблицы границ, без диапазона в скобках."""
    if key in SETTINGS_TEXT:
        label, unit = SETTINGS_TEXT[key][0], ""
    else:
        _lo, _hi, label, unit = SETTINGS_BOUNDS[key]
        unit = " " + unit.split(" (")[0]
    verb = _CHANGED[_SETTINGS_GENDER.get(key, "m")]
    old_s = _e(str(old)) if old not in (None, "", []) else "—"
    return f"✅ {_e(label)} успешно {verb}: {old_s} → <b>{_e(str(new))}</b>{_e(unit)}."


def settings_ssh_allow_added(entries: list) -> str:
    return ("✅ Адреса для SSH: добавлено "
            + ", ".join(f"<b>{_e(x)}</b>" for x in entries) + ".")


def settings_bad_value(key: str) -> str:
    lo, hi, label, unit = SETTINGS_BOUNDS[key]
    return f"⚠️ Нужно целое число в диапазоне {lo}–{hi} ({_e(unit)}). Попробуй ещё раз."
