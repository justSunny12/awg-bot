"""
texts.py — шаблоны сообщений (русский, с эмодзи) и форматтеры отображения.

Вынесено из логики, чтобы UI правился без копания в services/handlers.
Направление трафика (важно не перепутать): в awg dump rx = принято сервером
ОТ клиента = аплоад клиента; tx = отдано клиенту = даунлоад клиента. В БД
traffic_rx_* = аплоад, traffic_tx_* = даунлоад. В карточке показываем
↓ скачано = tx, ↑ загружено = rx.
"""

from __future__ import annotations

from awgbot.core import settings
import html

from awgbot.util import timeutil
from awgbot.core.enums import SubStatus, ActivationStatus, PeriodKind, FriendStatus


def _e(s) -> str:
    """Экранирование пользовательских строк (имён) для HTML parse_mode."""
    return html.escape(str(s))


# ─────────────────────────────────────────────────────────────────────────────
# Единицы
# ─────────────────────────────────────────────────────────────────────────────

def human_bytes(n: int) -> str:
    """Байты → человекочитаемо (Б/КБ/МБ/ГБ/ТБ)."""
    n = int(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            if unit == "Б":
                return f"{n} Б"
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


# ─────────────────────────────────────────────────────────────────────────────
# Потребление (в UI слово «трафик» заменено на «потребление», чтобы не пугать).
# Расход — автоформатом human_bytes; лимит — в ГБ с 2 знаками. Клиент/друг видят
# СУММУ (up+down) без разбивки; админ — тотал + разбивку ↑↓.
# ─────────────────────────────────────────────────────────────────────────────

# Дубль services.BYTES_PER_GB — НАМЕРЕННО: физическая константа (разойтись не
# может), а импорт services сюда тащил бы весь сервис-слой в текст-слой.
_BYTES_PER_GB = 1024 ** 3


def gb_str(num_bytes: int) -> str:
    """Лимит в ГБ, 2 знака: 100 → «100.00 ГБ». 0 трактуется вызывающим как безлимит."""
    return f"{num_bytes / _BYTES_PER_GB:.2f} ГБ"


def _limit_devices_str(limit: int) -> str:
    return "без ограничения" if not limit else str(limit)


def _limit_traffic_line(limit_bytes: int) -> str:
    """Строка о лимите потребления для отчётов о создании/изменении."""
    return ("Потребление не ограничено" if not limit_bytes
            else f"Лимит потребления: {gb_str(limit_bytes)}")


def _dev_traffic_line(dev_limit_bytes: int, profile_limit_bytes: int) -> str:
    """Строка о лимите потребления устройства для отчёта о создании.
    Свой лимит устройства → показываем его; иначе потребление ограничено лишь
    лимитом профиля (или ничем, если профиль безлимитный)."""
    if dev_limit_bytes:
        return f"Лимит потребления: {gb_str(dev_limit_bytes)}"
    if profile_limit_bytes:
        return (f"Потребление в рамках лимита профиля не ограничено "
                f"({gb_str(profile_limit_bytes)}/профиль)")
    return "Потребление в рамках лимита профиля не ограничено (профиль без ограничений)"


def device_created_report(dev_name: str, *, client_name: str = None,
                          device_count: int = 0, max_devices: int = 0,
                          dev_limit_bytes: int = 0, profile_limit_bytes: int = 0) -> str:
    """Отчёт о создании устройства. client_name — только для админа (у
    клиента/друга один профиль)."""
    head = f"✅ Устройство «{_e(dev_name)}» создано"
    if client_name:
        head += f" для профиля «{_e(client_name)}»"
    head += ".\n"
    head += f"Количество устройств: {device_count}/{_limit_devices_str(max_devices)},\n"
    head += _dev_traffic_line(dev_limit_bytes, profile_limit_bytes) + "."
    return head


def _limit_suffix(limit_bytes: int) -> str:
    """Хвост «(лимит X ГБ)» / «(без ограничения)»."""
    if limit_bytes == 0:
        return "(без ограничения)"
    return f"(лимит {gb_str(limit_bytes)})"


def consumption_line(used_sum: int, limit_bytes: int, *, blocked: bool,
                     until: str | None = None) -> str:
    """Строка потребления для клиента/друга: сумма расхода + лимит/статус.
    used_sum — сумма up+down. blocked — исчерпан ли лимит (доступ приостановлен).
    until — дата снятия («01.08.2026»)."""
    if blocked and limit_bytes:
        tail = f"лимит {gb_str(limit_bytes)}, исчерпан"
        if until:
            tail += f" — приостановлено до {until}"
        return f"Потребление за месяц: {human_bytes(used_sum)} ({tail})"
    return f"Потребление за месяц: {human_bytes(used_sum)} {_limit_suffix(limit_bytes)}"


def consumption_line_admin(rx: int, tx: int, limit_bytes: int) -> str:
    """Строка потребления для админа: тотал + разбивка ↑↓ + лимит."""
    total = int(rx) + int(tx)
    return (f"Потребление: {human_bytes(total)} "
            f"(↑ {human_bytes(rx)} | ↓ {human_bytes(tx)}) {_limit_suffix(limit_bytes)}")


def client_total_line(rx: int, tx: int, limit_bytes: int, bonus_bytes: int,
                      *, for_admin: bool) -> str:
    """Тотал клиента. С доп.квотой показываем разбивку «лимит + доп. до конца
    месяца» и клиенту, и админу (по договорённости — не словом «бонус»)."""
    total = int(rx) + int(tx)
    if limit_bytes == 0:
        base = "без ограничения"
    elif bonus_bytes:
        base = f"лимит {gb_str(limit_bytes)} + {gb_str(bonus_bytes)} до конца месяца"
    else:
        base = f"лимит {gb_str(limit_bytes)}"
    if for_admin:
        return (f"Потребление профиля за месяц: {human_bytes(total)} "
                f"(↑ {human_bytes(rx)} | ↓ {human_bytes(tx)}) ({base})")
    return f"Потребление за месяц: {human_bytes(total)} ({base})"


# ─────────────────────────────────────────────────────────────────────────────
# Устройства
# ─────────────────────────────────────────────────────────────────────────────

def device_label(dev, *, for_admin: bool = False) -> str:
    """Имя устройства + суффикс (APP) + индикатор онлайна + маркер блокировки.
    Маркер 🛑 показывается по ВИДИМОЙ для роли маске: тихий админ-блок пользователю
    не виден (устройство выглядит рабочим). Суффикс/маркер в имени не хранятся."""
    from awgbot.core import blocks
    online = timeutil.handshake_is_online(dev.last_handshake)
    dot = "🟢" if online else "🔴"
    name = _e(dev.name)
    if getattr(dev, "is_admin", False):
        name = f"{name} [Доступ к серверу]"
    elif dev.is_app:
        name = f"{name} <b>*</b>"
    marker = blocks.blocked_marker_device(int(dev.block_reason), for_admin=for_admin)
    return f"{marker}{dot} {name}"


def device_line(dev) -> str:
    """Строка устройства для списка: индикатор, имя, IP, последний коннект."""
    last = timeutil.fmt_handshake(dev.last_handshake)
    return f"{device_label(dev)} — {dev.address}, последний коннект: {last}"


def device_card_text(dev, *, for_admin: bool) -> str:
    """Карточка одного устройства: строка + потребление + причины блокировки.
    Причины фильтруются по роли: тихий админ-блок пользователю не показывается
    (для него устройство выглядит рабочим)."""
    from awgbot.core import blocks
    parts = [device_line(dev)]
    mask = int(dev.block_reason)
    traffic_blocked = bool(mask & int(blocks.DEVICE_TRAFFIC_ANY))
    if for_admin:
        parts.append(consumption_line_admin(
            dev.traffic_rx_month, dev.traffic_tx_month, dev.traffic_limit))
    else:
        used = int(dev.traffic_rx_month) + int(dev.traffic_tx_month)
        parts.append(consumption_line(
            used, dev.traffic_limit, blocked=traffic_blocked,
            until=timeutil.first_of_next_month_str() if traffic_blocked else None))
    reasons = blocks.device_reasons_ru(mask, for_admin=for_admin)
    if reasons:
        parts.append("⛔ Заблокировано: " + ", ".join(reasons))
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Срок подписки
# ─────────────────────────────────────────────────────────────────────────────

def subscription_block(client, *, for_admin: bool = False, show_pause: bool = True) -> str:
    """Блок срока: период + остаток. Учитывает приостановку (самоблок клиента и
    админский блок с паузой). Тихий (silent) админ-блок пользователю не виден —
    для него период/статус как будто ничего не произошло.
    for_admin=True — админ видит всё (включая silent-паузу и temp-бессрочность).
    show_pause=False — скрыть счётчик дней приостановки (друг ей не управляет)."""
    from awgbot.core import blocks
    mask = int(client.block_reason)
    paused = bool(mask & int(blocks.ClientBlock.PAUSED))
    mode = client.pause_mode or ""
    # видит ли ПОЛЬЗОВАТЕЛЬ эту паузу: самоблок — всегда; админская — только если
    # блок не тихий (есть видимый ADMIN_NOTIFIED). Админу видно всегда.
    silent_admin = bool(mask & int(blocks.ClientBlock.ADMIN_SILENT)) and \
        not bool(mask & int(blocks.ClientBlock.ADMIN_NOTIFIED))
    pause_visible = paused and (for_admin or mode == "user" or not silent_admin)

    if not client.period_end:
        # бессрочно — либо реально, либо temp (admin_open). Пользователю при
        # silent-паузе показываем как обычную активную бессрочную «легенду»? Нет:
        # admin_open зануляет period_end. Если пауза пользователю не видна, покажем
        # сохранённый конец как обычный период.
        if paused and mode == "admin_open" and not pause_visible and client.pause_saved_end:
            start = timeutil.parse_iso(client.period_start) if client.period_start else None
            end = timeutil.parse_iso(client.pause_saved_end)
            status = "🟢 активна"
            body = f"Период подписки: {timeutil.fmt_period(start, end)}" if start else \
                   f"Период подписки: до {timeutil.fmt_dt(end)}"
            return f"Статус подписки: {status}\n{body}\nДо истечения: {timeutil.fmt_remaining(end)}"
        status = "🟢 активна" if client.status == SubStatus.ACTIVE else "🔴 истекла"
        if pause_visible and mode == "admin_open":
            status = "⏸ приостановлено администратором"
            return (f"Статус подписки: {status}\n"
                    "Период подписки: временно бессрочный "
                    "(пересчитается при снятии блокировки)")
        return f"Статус подписки: {status}\nПериод подписки: бессрочно"

    if not client.period_start:
        # аномалия данных: period_end есть, period_start — нет (не должно
        # случаться при нормальной работе, но не показываем голый прочерк)
        status = "🟢 активна" if client.status == SubStatus.ACTIVE else "🔴 истекла"
        return f"Статус подписки: {status}\nПериод подписки: дата начала не определена"
    start = timeutil.parse_iso(client.period_start)
    end = timeutil.parse_iso(client.period_end)

    if pause_visible:
        if mode == "user":
            status = "⏸ приостановлено пользователем"
        else:
            status = "⏸ приостановлено администратором"
    else:
        status = "🟢 активна" if client.status == SubStatus.ACTIVE else "🔴 истекла"

    lines = [f"Статус подписки: {status}"]
    period_line = f"Период подписки: {timeutil.fmt_period(start, end)}"
    if pause_visible and mode == "user":
        period_line += f" (+ до {int(client.pause_reserved_days)} дней приостановки)"
    elif pause_visible and mode == "admin_fixed":
        period_line += " (пересчитается при снятии блокировки)"
    lines.append(period_line)
    if not pause_visible:
        lines.append(f"До истечения: {timeutil.fmt_remaining(end)}")
    # счётчик самоблоков клиента (только годовая, только пользовательский режим).
    # Другу не показываем — паузой управляет владелец, другу счётчик бесполезен.
    if show_pause and client.period_kind == PeriodKind.YEAR:
        used = int(client.pause_used_days)
        if paused and mode == "user":
            used += int(client.pause_reserved_days)
        avail = max(0, settings.get_int("pause.pause_max_total_days", 28) - used)
        until = f" до {timeutil.fmt_dt(end)}" if end else ""
        lines.append(f"Приостановка: доступно {avail}/{settings.get_int("pause.pause_max_total_days", 28)} дн.{until}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Карточка клиента (для админа и для самого клиента)
# ─────────────────────────────────────────────────────────────────────────────

def friend_panel(dev, host_client) -> str:
    """Панель друга: инфо про УСТРОЙСТВО (не про юзера) + подписка хозяина.
    Потребление — суммой (up+down), с лимитом устройства и статусом блокировки."""
    from awgbot.core import blocks
    online = timeutil.handshake_is_online(dev.last_handshake)
    head = f"📱 Устройство: {_e(dev.name)}"
    online_line = "Сейчас: " + ("🟢 онлайн" if online else "🔴 оффлайн")
    sub = subscription_block(host_client, show_pause=False)
    used = int(dev.traffic_rx_month) + int(dev.traffic_tx_month)
    blocked = bool(int(dev.block_reason) & int(blocks.DEVICE_TRAFFIC_ANY))
    tr = consumption_line(used, dev.traffic_limit, blocked=blocked,
                          until=timeutil.first_of_next_month_str() if blocked else None)
    parts = [f"{head}\n{online_line}", sub, tr]
    # причины блокировки — как у клиента (тихий админ-блок другу не виден)
    reasons = blocks.device_reasons_ru(int(dev.block_reason), for_admin=False)
    if reasons:
        parts.append("⛔ Заблокировано: " + ", ".join(reasons))
    return "\n\n".join(parts)


FRIEND_ALREADY_USER = (
    "Ты уже пользуешься этим ботом как владелец доступа 🙂\n"
    "Одному человеку — одна роль. Приглашение друга можно активировать только "
    "с аккаунта, у которого ещё нет доступа."
)


def friend_activated(device_name: str) -> str:
    return (f"Готово! Тебе передали устройство «{_e(device_name)}» 🎉\n"
            "Ниже — панель управления им.")


def friend_activated_host_notice(device_name: str, who: str) -> str:
    return f"👤 Друг ({_e(who)}) активировал устройство «{_e(device_name)}»."


# Контекстные «завершители» под выданным контентом (баббл с кнопкой «В меню»).
def finish_link(name: str) -> str:
    return (f"☝️ Вот, держи — ссылка для подключения твоего устройства "
            f"«{_e(name)}». Вставь её в приложение AmneziaVPN.")


def finish_qr(name: str) -> str:
    return (f"☝️ Вот, держи — QR-код для твоего устройства «{_e(name)}».\n"
            "В AmneziaVPN: «＋» → «Создать из QR-кода» и наведи камеру на "
            "анимацию.")


def finish_file(name: str) -> str:
    return (f"☝️ Вот, держи — файл настроек для твоего устройства «{_e(name)}». "
            "Импортируй его в приложение AmneziaVPN.")


CONNECT_METHOD_ASK = "Как планируешь подключить устройство?"
FINISH_CLIENT_INVITE = (
    "☝️ Выше — ссылка-приглашение. Перешли её человеку, чтобы он активировал доступ.\n\n"
    "❗️ После возврата в меню это сообщение исчезнет — повторно сгенерировать его "
    "будет можно из профиля клиента, до момента принятия приглашения. Уже "
    "пересланное сообщение останется рабочим."
)
FINISH_FRIEND_INVITE = "☝️ Выше — приглашение для друга. Перешли его — друг активирует и получит своё устройство."


ADD_FOR_WHOM = (
    "Для кого создаём устройство?\n\n"
    "<b>\U0001F4F1 Себе</b> — получишь данные для подключения прямо сейчас.\n"
    "<b>\U0001F464 Другу</b> — сгенерирую приглашение в бота. Друг активирует его "
    "и сможет <b>сам</b> получать данные для подключения здесь, в боте — "
    "тебе не придётся пересылать их ему вручную."
)


FRIEND_DEVICE_DELETED_GENERIC = (
    "Устройство, которым ты управлял, удалено владельцем — доступ по нему больше не работает."
)


def friend_invite_message(device_name: str, code: str, bot_username: str) -> str:
    link = f"https://t.me/{bot_username}?start={code}"
    return (f"Приглашение на устройство «{_e(device_name)}» готово 👇\n"
            "Перешли другу — он активирует и получит управление этим устройством:\n\n"
            f"{link}\n\n"
            f"Или пусть отправит боту: <code>/code {code}</code>")


def friend_marker(dev) -> str:
    if dev.friend_status == FriendStatus.ACTIVE:
        return "👤 Передано другу"
    if dev.friend_status == FriendStatus.PENDING:
        return "⏳ Приглашение другу ждёт активации"
    return ""


TRANSFER_FRIEND_WARNING = (
    "<blockquote>Передавая устройство другу, ты отдаёшь ему это подключение. "
    "Пользоваться одним подключением с нескольких устройств одновременно "
    "нормально не выйдет — каждому нужна своя ссылка.\n"
    "Если сам пользуешься этим устройством — сначала заведи себе новое.</blockquote>\n"
    "Передать устройство «{name}» другу?"
)


def client_card(client, devices, traffic, online: bool, *, for_admin: bool) -> str:
    """Полная карточка: имя, подписка, онлайн, потребление, устройства."""
    head = f"👤 {_e(client.name)}"
    if for_admin and client.activation_status == ActivationStatus.PENDING:
        head += "  ⏳ ждёт активации"
    online_line = "Сейчас: " + ("🟢 онлайн" if online else "🔴 оффлайн")

    sub = subscription_block(client, for_admin=for_admin)

    # потребление за месяц: клиенту — сумма, админу — с разбивкой ↑↓; тотал-лимит
    tr = client_total_line(
        traffic["rx_month"], traffic["tx_month"],
        client.traffic_limit, client.bonus_bytes, for_admin=for_admin)

    lim = client.device_limit
    limit_line = (f"Устройств: {len(devices)} (без ограничения)" if lim == 0
                  else f"Устройств: {len(devices)} из {lim}")

    dev_block = "\n".join("  " + device_label(d, for_admin=for_admin) for d in devices)

    limit_and_devs = f"{limit_line}\n{dev_block}" if dev_block else limit_line
    parts = [f"{head}\n{online_line}", sub, tr, limit_and_devs]
    # причины блокировки клиента (тихий админ-блок пользователю не виден)
    from awgbot.core import blocks
    reasons = blocks.client_reasons_ru(int(client.block_reason), for_admin=for_admin)
    if reasons:
        parts.insert(1, "⛔ Заблокирован: " + ", ".join(reasons))
    return "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Списки и статусы
# ─────────────────────────────────────────────────────────────────────────────


def _fmt_age(seconds) -> str:
    """Человекочитаемый возраст данных: «40 сек» / «3 мин» / «2 ч назад»."""
    if seconds is None:
        return ""
    s = int(seconds)
    if s < 90:
        return f"{s} сек назад"
    if s < 5400:
        return f"{s // 60} мин назад"
    return f"{s // 3600} ч назад"


def admin_panel(st: dict) -> str:
    """Шапка админ-меню: компактный статус из кэша (ноль docker exec).
    st — из services.server_status_cached(); метрики железа (CPU/RAM/диск хоста)
    бот снимает локально (/proc, statvfs); показываем с возрастом. None-поля — «…»."""
    if st.get("ok") is None:
        dot = "…"
    else:
        dot = "🟢 работает" if st["ok"] else "🔴 не отвечает"
    # Блок «сервер»: статус, аптайм и метрики — каждый своей строкой.
    head = [f"🖥 Сервер: {dot}"]
    if st.get("uptime"):
        head.append(f"⬆️ Аптайм: {st['uptime']}")
    if st.get("cpu") is not None or st.get("ram") is not None or st.get("disk") is not None:
        def _p(v):
            return f"{v:.0f}%" if v is not None else "?"
        metrics = (f"📈 CPU {_p(st.get('cpu'))} · RAM {_p(st.get('ram'))} "
                   f"· Диск {_p(st.get('disk'))}")
        age = _fmt_age(st.get("age_seconds"))
        if age:
            metrics += f" · {age}"
        head.append(metrics)
    elif st.get("age_seconds") is None:
        head.append("📈 Метрики: нет данных (монитор ещё не сделал первый замер)")

    # Группы разделяем пустой строкой: [сервер] / [онлайн] / [потребление].
    groups = ["\n".join(head)]
    if st.get("online_count") is not None:
        groups.append(f"📶 Устройств онлайн: {st['online_count']}")
    if st.get("traffic_rx") is not None:
        rx, tx = int(st["traffic_rx"]), int(st["traffic_tx"])
        groups.append(f"📊 Потребление за месяц (все): {human_bytes(rx + tx)} "
                      f"(↑ {human_bytes(rx)} | ↓ {human_bytes(tx)})")
    return "🛠 <b>Панель администратора</b>\n\n" + "\n\n".join(groups)


# ─────────────────────────────────────────────────────────────────────────────
# Приветствие / активация
# ─────────────────────────────────────────────────────────────────────────────

def _pause_visibility(client, *, for_admin: bool = False) -> tuple:
    """(paused, mode, pause_visible) — видит ли ПОЛЬЗОВАТЕЛЬ текущую паузу:
    самоблок — всегда; админская — только если блок не тихий. Админу видно
    всегда. Общая логика для subscription_status_only/subscription_manage_text
    и (частично) subscription_block."""
    from awgbot.core import blocks
    mask = int(client.block_reason)
    paused = bool(mask & int(blocks.ClientBlock.PAUSED))
    mode = client.pause_mode or ""
    silent_admin = bool(mask & int(blocks.ClientBlock.ADMIN_SILENT)) and \
        not bool(mask & int(blocks.ClientBlock.ADMIN_NOTIFIED))
    pause_visible = paused and (for_admin or mode == "user" or not silent_admin)
    return paused, mode, pause_visible


def subscription_manage_text(client, traffic, online: bool, device_count: int) -> str:
    """Инфобокс «Управлять подпиской» (было «Моя информация»): имя, текущий
    (онлайн/оффлайн) статус, статус/период/срок/приостановка подписки,
    потребление и лимит, количество устройств. Без итогового списка устройств
    по имени — это уже есть в «Мои устройства»."""
    lines = [f"👤 {_e(client.name)}",
            "Текущий статус: " + ("🟢 онлайн" if online else "🔴 оффлайн")]

    paused, mode, pause_visible = _pause_visibility(client)
    sub_lines = [f"Статус подписки: {subscription_status_only(client)}"]
    if client.period_end:
        start = timeutil.parse_iso(client.period_start) if client.period_start else None
        end = timeutil.parse_iso(client.period_end)
        period_str = timeutil.fmt_period(start, end) if start else f"до {timeutil.fmt_dt(end)}"
        sub_lines.append(f"Период подписки: {period_str}")
        if not pause_visible:
            sub_lines.append(f"До истечения: {timeutil.fmt_remaining(end)}")
    else:
        sub_lines.append("Период подписки: бессрочно")
    if pause_visible:
        if mode == "user":
            reserved = int(client.pause_reserved_days)
            sub_lines.append(f"Приостановка: до {reserved} дн. (можешь возобновить раньше)")
        elif mode == "admin_fixed":
            sub_lines.append("Приостановка: администратором, срок пересчитается при снятии")
        else:
            sub_lines.append("Приостановка: администратором, бессрочно")
    elif paused:
        # тихий админ-блок — пользователю паузу не показываем вовсе (уже
        # учтено в subscription_status_only через pause_visible=False)
        pass
    lines.append("\n".join(sub_lines))

    lines.append(client_total_line(traffic["rx_month"], traffic["tx_month"],
                                   client.traffic_limit, client.bonus_bytes, for_admin=False))

    lim = client.device_limit
    lines.append(f"Устройств: {device_count} (без ограничения)" if lim == 0
                else f"Устройств: {device_count} из {lim}")
    return "\n\n".join(lines)


def subscription_status_only(client) -> str:
    """Только статус подписки (без периода/дат — те в «Управлять подпиской»).
    Для лёгкого инфобокса главного меню клиента."""
    _, mode, pause_visible = _pause_visibility(client)
    if pause_visible:
        return "⏸ приостановлено пользователем" if mode == "user" else "⏸ приостановлено администратором"
    return "🟢 активна" if client.status == SubStatus.ACTIVE else "🔴 истекла"


def server_status_client(ok: bool) -> str:
    return "🟢 VPN-сервер работает нормально" if ok else "🔴 VPN-сервер не отвечает"


def greeting_client(client, server_ok: bool, slots: tuple[int, int] = None) -> str:
    """Инфобокс главного меню клиента: приветствие, статус сервера (отдельным
    абзацем сразу после приветствия — пустая строка с обеих сторон), статус
    подписки (только статус — период/даты в «Управлять подпиской»), максимум
    устройств и текущее количество."""
    text = (f"Привет, {_e(client.name)}! 👋\n\n"
           f"{server_status_client(server_ok)}\n\n"
           f"Статус подписки: {subscription_status_only(client)}")
    if slots is not None:
        used, limit = slots
        if limit == 0:                             # безлимит
            text += f"\n\nУстройств добавлено: {used} (без ограничения)."
        elif used == 0:
            text += f"\n\nВсего можно добавить до {limit} {plural_ru(limit, 'устройства', 'устройств', 'устройств')}. Пока не добавлено ни одного."
        else:
            text += f"\n\nУстройств добавлено: {used} из {limit}."
    return text


def device_slots_line(used: int, limit: int) -> str:
    if limit == 0:                                 # безлимит
        return f"Устройств добавлено: {used}. Можно добавлять без ограничения."
    if used == 0:
        return f"Всего можно добавить до {limit} {plural_ru(limit, 'устройства', 'устройств', 'устройств')}. Пока не добавлено ни одного."
    if used >= limit:
        return (f"Устройств добавлено: {used} из {limit}. Лимит исчерпан — "
                f"чтобы добавить новое, сначала удали одно из существующих.")
    return f"Устройств добавлено: {used} из {limit}. Можно добавить ещё {limit - used}."


DELETE_ONLY_DEVICE_WARNING = (
    "⚠️ Это твоё <b>единственное</b> устройство.\n\n"
    "Если удалить — VPN сразу перестанет работать. И, если прямо сейчас ты "
    "пользуешься Telegram только через этот VPN, ты потеряешь доступ и к боту — "
    "и не сможешь подключиться заново сам.\n\n"
    "Точно удалить?"
)

DELETE_DEVICE_CONFIRM = (
    "<blockquote>При удалении устройства его ссылка для подключения станет "
    "неактивной, и VPN по ней работать перестанет!\n"
    "Если потом захочешь добавить его снова — ссылка будет новая.</blockquote>\n"
    "Удалить устройство «{name}»?"
)

HELP_INTRO = "Нужна помощь с настройкой? Выбери своё устройство:"

APP_DEVICE_EXPLAIN = (
    "\n\n<blockquote>Это устройство создано во внешнем приложении, поэтому бот не "
    "может выдать для него ссылку (приватный ключ хранится только на самом "
    "устройстве). Пришли строку подключения (vpn://…) из приложения кнопкой ниже — "
    "и бот сможет выдавать ссылку/файл. Либо удали его и добавь новое через бота.\n"
    "По той же причине его пока нельзя передать другу: другу нужна ссылка, а её "
    "бот выдать не может. Пропиши строку подключения — и передача станет доступна.</blockquote>"
)

APP_DEVICE_PICK_DIALOG = (
    "Ой, не помню, чтобы это устройство добавлялось через меня 😳\n"
    "Может, администратор добавил его из приложения?\n"
    "Если у тебя есть ссылка для подключения этого устройства — скинь её мне, всё починим.\n"
    "Если ссылка потерялась — восстановить не получится 🙁 "
    "Но ты можешь удалить это устройство и добавить как новое."
)

RESTORE_WRONG_DEVICE = (
    "Хм, эта ссылка не от того устройства 🤔\n"
    "Пришли строку подключения именно этого устройства из приложения."
)
RESTORE_BAD_LINK = (
    "Не получилось разобрать ссылку. Пришли строку целиком — она начинается "
    "с vpn:// (её можно скопировать в приложении AmneziaVPN)."
)
ADMIN_FA_PICK = (
    "Выбери устройство полного доступа. Ориентируйся на имя из приложения "
    "(у full-access обычно «Admin [платформа]») и IP в скобках:"
)
ADMIN_FA_HINT = (
    "⚠️ <b>Не назначено устройство полного доступа.</b>\n\n"
    "Ссылка полного доступа из приложения Amnezia позволяет управлять сервером "
    "(разворачивать/поднимать VPN), даже если с твоим админским устройством что-то "
    "случится. Сейчас она не сохранена — при проблемах ты рискуешь потерять "
    "управление сервером из приложения.\n\n"
    "Назначь устройство полного доступа, чтобы обезопаситься."
)
ADMIN_FA_CLEAR_WARNING = (
    "⚠️ <b>Снять метку полного доступа?</b>\n\n"
    "Сохранённая ссылка будет <b>безвозвратно утрачена</b> — восстановить её из "
    "бота не получится. Устройство вернётся в общий пул (обычное управление).\n\n"
    "Снимаю пометку — точно?"
)

def fa_transfer_warning(current_name: str) -> str:
    return (
        "⚠️ <b>Перенос полного доступа</b>\n\n"
        f"Сейчас полный доступ назначен устройству <b>{current_name}</b>. "
        "Оно потеряет доступ к серверу: его ссылка будет <b>стёрта безвозвратно</b>, "
        "а само устройство станет обычным — чтобы вернуть ему доступ, "
        "придётся заново прикрепить ссылку.\n\n"
        "Полный доступ может быть только у одного устройства. Продолжить перенос?"
    )


def fa_assign_confirm(name: str, address: str, looks_admin: bool) -> str:
    head = (f"Назначаю полный доступ устройству:\n<b>{name}</b> [{address}]\n\n")
    if not looks_admin:
        head += ("⚠️ Обычно устройство полного доступа называется «Admin […]», а это — "
                 f"«{name}». Убедись, что выбрал именно то устройство из приложения.\n\n")
    return head.rstrip()

ADMIN_FA_CHANGE_WARNING = (
    "⚠️ <b>Заменить ссылку полного доступа?</b>\n\n"
    "Прежняя ссылка будет <b>безвозвратно утрачена</b> — восстановить её из бота "
    "не получится. Меняй только если пересоздал awg-контейнер и получил новую.\n\n"
    "Продолжить?"
)

NEED_ENCRYPTION_GUIDE = (
    "🔒 Чтобы хранить ссылку полного доступа, сначала включи шифрование "
    "резервных копий — тем же ключом шифруется и эта ссылка (иначе root-доступ "
    "к серверу утёк бы в бэкапе через Telegram).\n\n"
    "На сервере, где стоит бот:\n"
    "<code>sudo awg-bot reconfigure</code>\n"
    "→ на шаге «шифрование резервных копий» ответь «да» и задай парольную фразу "
    "(или сгенерируй ключ).\n\n"
    "Либо напрямую:\n"
    "<code>cd /opt/awg-bot && sudo ./venv/bin/python -m tools.manage_secrets</code>\n\n"
    "Бот перезапустится сам и подхватит ключ — после этого пришли ссылку снова."
)

ADMIN_LINK_WARNING = (
    "⚠️ <b>Это ссылка ПОЛНОГО ДОСТУПА к серверу.</b>\n\n"
    "Кто получит её — получит полное управление хостом: сможет сменить root-пароль "
    "и заблокировать тебя, отозвать её нельзя. Отдавай только на СВОЁ доверенное "
    "устройство и никому не пересылай.\n\n"
    "Точно выдать?"
)

RESTORE_FULL_ACCESS_SAVED = (
    "✅ Сохранил ссылку полного доступа для этого устройства — помечено "
    "«Полный доступ» (а из текущего чата — удалил, в целях безопасности).\n"
    "Теперь по нему можно получить ссылку/QR/файл, а приложение Amnezia "
    "развернёт подключение само."
)


def limit_changed_notice(old: int, new: int) -> str:
    def _fmt(v):
        return "без ограничения" if v == 0 else str(v)
    return (f"Максимальное количество устройств для тебя изменено. "
            f"Было: {_fmt(old)}, стало: {_fmt(new)}.")

def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение по числу — переиспользует хелпер из timeutil
    (единая логика на весь проект). 1 устройство, 2 устройства, 5 устройств."""
    return timeutil._plural_ru(n, (one, few, many))


def _devices_word(n: int) -> str:
    return plural_ru(n, "устройство", "устройства", "устройств")


def _slots_phrase(count: int, limit: int) -> str:
    """«m подключённых устройств из n возможных» / «…без ограничения».
    Склоняем и «устройство» по m, и «возможного/возможных» по n."""
    word = _devices_word(count)
    connected = plural_ru(count, "подключённое", "подключённых", "подключённых")
    if limit == 0:
        return f"Теперь у тебя {count} {connected} {word} (без ограничения)."
    possible = plural_ru(limit, "возможного", "возможных", "возможных")
    return f"Теперь у тебя {count} {connected} {word} из {limit} {possible}."


def reassign_donor_notice(name: str, count: int, limit: int) -> str:
    return (f"Устройство «{_e(name)}» удалено из твоего профиля администратором.\n"
            + _slots_phrase(count, limit))


def reassign_recipient_notice(name: str, count: int, limit: int, *,
                              recipient_is_admin: bool = False) -> str:
    """recipient_is_admin=True — получатель сам админ (взял бесхозное устройство
    себе): «добавлено ... администратором» звучало бы странно (сам себе).
    Обычному клиенту — как и раньше, с указанием, что сделал админ."""
    tail = "" if recipient_is_admin else " администратором"
    return (f"Устройство «{_e(name)}» добавлено в твой профиль{tail}.\n"
            + _slots_phrase(count, limit))


def reassign_recipient_notice_with_slot(name: str, count: int, limit: int, *,
                                        recipient_is_admin: bool = False) -> str:
    tail = "" if recipient_is_admin else " администратором"
    return (f"Устройство «{_e(name)}» добавлено в твой профиль{tail}, "
            "тебе также добавлен слот.\n"
            + _slots_phrase(count, limit))


INVITE_FORWARD_TEMPLATE = (
    "Привет!\n"
    "Тебе одобрен доступ в свободный интернет 😊\n"
    "Для получения настроек переходи по ссылке и жми \"СТАРТ\" — расскажу, "
    "что делать дальше\n"
    "{link}"
)

ACTIVATION_OK = "Готово! Доступ активирован. 🎉"
ACTIVATION_INVALID = (
    "Не помню такого кода в списках, что-то ты путаешь...\n"
    "Как найдёшь правильный код — пиши, пообщаемся 🙂"
)
ACTIVATION_ALREADY = "У тебя уже есть доступ."

COLD_START_GREETING = (
    "Привет!\n"
    "Ой, что-то я тебя не припоминаю 😳\n"
    "Если ты перешёл по ссылке-приглашению, но код не подхватился (так бывает — "
    "Telegram иногда не передаёт его с первого раза), просто отправь мне код "
    "командой <code>/code КОД</code> — сверю по спискам."
)

CODE_NO_ARG = "Отправь код после команды, вот так: <code>/code твой_код</code>"

def activated_admin_notice(name: str, who: str) -> str:
    return f"🎉 Профиль «{_e(name)}» активировал доступ ({_e(who)})."

_PERIOD_WORD = {"day": "на день", "week": "на неделю",
                "month": "на месяц", "year": "на год"}


def client_created_report(name: str, *, device_limit: int, traffic_limit_bytes: int,
                          period_kind, period_end) -> str:
    """Констатирующий результат создания профиля: имя, лимиты, срок подписки.
    Остаётся в чате (не транзиентный invite-контент)."""
    dev = f"до {device_limit} устройств" if device_limit else "количество устройств не ограничено"
    if traffic_limit_bytes:
        gb = traffic_limit_bytes / _BYTES_PER_GB
        gb_txt = f"{gb:.0f}" if gb == int(gb) else f"{gb:.2f}"
        traf = f"до {gb_txt} ГБ"
    else:
        traf = "потребление не ограничено"
    if period_end is None:
        sub = "бессрочная подписка"
    else:
        word = _PERIOD_WORD.get(str(period_kind), "")
        sub = f"подписка {word} до {timeutil.fmt_dt(period_end)}".replace("  ", " ").strip()
    return (f"✅ Профиль «{_e(name)}» создан ({dev}, {traf}), {sub}.\n"
            "Повторный выпуск приглашения возможен из меню клиента до его активации.")

LIMIT_REACHED = "Достигнут лимит устройств."
EXTEND_KEEP_QUESTION = "Сохранить неистраченный остаток ({remainder})?"



TRAFFIC_LIMIT_CLIENT_ASK = (
    "Задай лимит потребления профиля на месяц — это общий потолок по всем его "
    "устройствам.\n\nВведи целое число гигабайт (например 100). "
    "0 — без ограничения.")

def traffic_limit_device_ask(profile_limit_bytes: int) -> str:
    """Приглашение задать лимит устройства. Если у профиля есть свой лимит —
    показываем его («в пределах лимита профиля: N ГБ»); если профиль безлимитный
    (0) — фразу в скобках опускаем целиком."""
    base = ("Задай лимит потребления устройства на месяц.\n\nВведи целое число "
            "гигабайт (например 50). 0 — без ограничения")
    if profile_limit_bytes and int(profile_limit_bytes) > 0:
        return f"{base} (в пределах лимита профиля: {gb_str(profile_limit_bytes)})."
    return f"{base}."

TRAFFIC_LIMIT_BAD = "Нужно целое число гигабайт (0 — без ограничения). Попробуй ещё раз:"


# ── «Продли на пару недель» (самостоятельная отсрочка) ──────────────────────────

def grace_activated_client(days: int, end) -> str:
    return (f"🙏 Готово! Подписка продлена на {days} дн. — до {end}.\n"
            "Эти дни вычтутся из следующего продления.")

GRACE_STALE = "Это предложение уже неактуально."

def grace_activated_admin(name: str, days: int) -> str:
    return f"🙏 Профиль «{_e(name)}» активировал отсрочку на {days} дн."


# ── Приостановка: инфобоксы диалога ──────────────────────────────────────────

def pause_ask(available_days: int, used: int, total: int) -> str:
    """Инфобокс перед выбором длительности: сколько доступно и почему."""
    why = ""
    if used > 0:
        why = f"\n\nУже израсходовано {used}/{total} дн. приостановки за действующий период подписки."
    return (f"⏸ Подписку можно приостановить максимум на {available_days} дн.\n\n"
            "Пока подписка на паузе, её срок не тикает. Возобновить можно в любой "
            "момент. Тогда неиспользованные дни приостановки вернутся обратно — "
            "их можно будет использовать позже, а зачтётся только фактическое "
            "количество дней паузы (даже 1 минута паузы считается как целый день)." + why +
            "\n\nНа сколько дней приостановить?")


def pause_warning(days: int) -> str:
    """Предупреждение перед подтверждением: пауза отключает VPN, а выйти можно
    только через этот бот. Если Telegram доступен лишь через этот VPN — клиент
    рискует запереться. (Аварийный e-mail-выход добавит фича 2.)"""
    return ("⚠️ <b>Прежде, чем мы продолжим:</b>\n\n"
            "На время паузы VPN отключается. Выйти из приостановки досрочно можно "
            "только кнопкой «Возобновить» здесь, в этом боте.\n\n"
            "Если ты заходишь в Telegram <b>только через этот VPN</b>, то после "
            "постановки на паузу потеряешь доступ и к боту — и не сможешь снять "
            "паузу сам.\n\n"
            "Продолжить?")


def pause_emergency_code(code: str, address: str) -> str:
    """Аварийный код email-выхода — показывается после входа в паузу, если фича
    включена. Клиент, заперевшийся без Telegram, шлёт этот код письмом."""
    return (f"🆘 <b>Аварийный выход без Telegram</b>\n\n"
            f"Если потеряешь доступ к боту (Telegram только через этот VPN), "
            f"отправь письмо на <code>{_e(address)}</code>, указав в теме письма "
            f"только этот код:\n\n<code>{_e(code)}</code>\n\n"
            f"Доступ восстановится автоматически. Код одноразовый и действует, "
            f"пока активна приостановка.\n\n"
            f"<b>Сохрани код и e-mail в заметках на всякий случай.</b>")


def pause_entered_summary(until: str) -> str:
    """Итог входа в паузу — остаётся в чате как результат действия (промежуточные
    шаги стираются). until — дата авто-возобновления (DD.MM.YYYY HH:MM)."""
    return (f"Подписка приостановлена до {until}.\n"
            "При необходимости можно возобновить досрочно через бота.")

def pause_unavailable() -> str:
    """Лимит берём из конфига (не хардкод): при смене PAUSE_MAX_TOTAL_DAYS текст
    иначе называл бы пользователю неверную цифру. Функция (а не константа) —
    config импортируется лениво, как и в остальных динамических текстах модуля."""
    return ("Приостановка сейчас недоступна: она возможна только для годовой "
            f"подписки, и суммарно не более {settings.get_int("pause.pause_max_total_days", 28)} дней за период.")


def pause_limit_exhausted() -> str:
    """Годовая подписка, но доступных дней приостановки не осталось."""
    return "Лимит дней приостановки в текущем периоде исчерпан."

def pause_resume_ask(actual: int, reserved: int) -> str:
    """Инфобокс подтверждения досрочного выхода из паузы — явно указываем,
    что спишутся ФАКТИЧЕСКИЕ дни, а не весь зарезервированный остаток."""
    if reserved:
        return (f"▶️ Возобновить сейчас? Будет использовано {actual} из "
                f"{reserved} зарезервированных дней приостановки — "
                f"неиспользованный остаток вернётся в подписку.")
    return f"▶️ Возобновить сейчас? Приостановка длилась {actual} дн."


def pause_resumed_self(actual_days: int, new_end) -> str:
    return (f"▶️ Подписка возобновлена. Использовано {actual_days} дн. паузы, "
            f"активна до {timeutil.fmt_dt(new_end)}.")

# ── Статус awg-сервиса (монитор) ─────────────────────────────────────────────
HB_SERVER_DOWN = "🔴 Сервис AWG недоступен — контейнер не отвечает."
HB_SERVER_UP = "🟢 Сервис AWG снова в строю."


# ── Обновления бота (self-update) ────────────────────────────────────────────
# Лимит сообщения Telegram — 4096 символов. Changelog кладём в сворачиваемую
# цитату; если тело релиза + шапка не влезают, режем тело по границе строки.
_TG_LIMIT = 4096


def _changelog_block(body: str, header: str) -> str:
    """<blockquote expandable> с телом релиза, усечённым под лимит Telegram.

    Тело экранируем целиком ДО обрезки (рвать нечего — тегов внутри нет), режем
    по границам строк под остаток бюджета. Обрезали — честный хвост. Возвращает
    готовую цитату (или пустую строку, если тела нет)."""
    body = (body or "").strip()
    if not body:
        return ""
    tail = "\n…\n(изменения обрезаны)"
    # бюджет под содержимое цитаты = лимит − шапка − теги − запас на хвост
    budget = _TG_LIMIT - len(header) - len("<blockquote expandable></blockquote>") \
        - len(tail) - 16
    esc = _e(body)
    if len(esc) <= budget:
        inner = esc
    else:
        # режем исходный текст по строкам, затем экранируем срез
        kept, used = [], 0
        for line in body.split("\n"):
            add = len(_e(line)) + 1
            if used + add > budget:
                break
            kept.append(line)
            used += add
        inner = _e("\n".join(kept)) + tail
    return f"<blockquote expandable>{inner}</blockquote>"


def update_available(tag: str, body: str) -> str:
    """Уведомление о доступной новой версии (следующей ступени)."""
    header = f"Доступна новая версия бота: {_e(tag)}\nСписок изменений:\n"
    return header + _changelog_block(body, header)


def update_current_ok(installed: str) -> str:
    """Админ-проверка: обновляться не на что."""
    return f"Текущая версия бота ({_e(installed)}) актуальна"


def update_admin_available(installed: str, tag: str, body: str) -> str:
    """Админ-проверка: доступна следующая версия."""
    header = (f"Текущая версия бота {_e(installed)}.\n"
              f"Доступно обновление до {_e(tag)}\nСписок изменений:\n")
    return header + _changelog_block(body, header)


def update_wait(tag: str) -> str:
    """Единственное сообщение на время обновления (цепочка до него стёрта)."""
    return f"⏳ Обновление до {_e(tag)}, дождись завершения."


def update_failed(reason: str) -> str:
    return f"⚠️ Не удалось обновить: {_e(reason)}"


def update_applied(tag: str, body: str) -> str:
    """Итог успешного self-update (после рестарта): остаётся в истории.
    Changelog установленной версии — под катом, как в уведомлении."""
    header = f"✅ Бот успешно обновлен до {_e(tag)}\nСписок изменений:\n"
    return header + _changelog_block(body, header)


def update_not_applied(tag: str, installed: str) -> str:
    return (f"⚠️ Обновление до {_e(tag)} не применилось — версия осталась "
            f"{_e(installed)}. Смотри журнал: journalctl -u awg-bot-selfupdate*")


# ── Экран настроек ───────────────────────────────────────────────────────────
SETTINGS_ROOT = "⚙️ <b>Настройки</b>\n\nВыбери раздел. Изменения применяются сразу."
SETTINGS_NOTIFY = ("🔔 <b>Уведомления</b>\n\nТихие часы (ночью без звука), алерты "
                   "о загрузке хоста и уведомления админу о событиях клиентов.")
SETTINGS_SUBS = "💳 <b>Параметры подписок</b>\n\nЛимиты и сроки, общие для всех клиентов."
SETTINGS_MON = ("📊 <b>Мониторинг</b>\n\nЧастота опроса, чувствительность алертов "
                "и поведение при простое AWG.")
SETTINGS_BACKUP = ("💾 <b>Резервное копирование</b>\n\nРасписание автоматического "
                  "резервного копирования и ручной запуск.")
SETTINGS_SVC = "🔄 <b>Сервис</b>\n\nПерезапуск AmneziaWG и самого бота."
SETTINGS_UPD = ("⬆️ <b>Обновления бота</b>\n\nАвтоуведомления, периодичность проверки "
                "и ручная проверка новой версии.")

# границы валидации ввода: dotted-ключ → (мин, макс, подпись, единица)
SETTINGS_BOUNDS = {
    "quiet_hours.quiet_hours_start": (0, 23, "Начало тихих часов", "час (0–23)"),
    "quiet_hours.quiet_hours_end": (0, 23, "Конец тихих часов", "час (0–23)"),
    "resource_alerts.thresholds_percent.cpu": (1, 100, "Порог CPU", "% (1–100)"),
    "resource_alerts.thresholds_percent.ram": (1, 100, "Порог RAM", "% (1–100)"),
    "resource_alerts.thresholds_percent.disk": (1, 100, "Порог диска", "% (1–100)"),
    "limits.traffic_bonus_gb": (1, 100000, "Бонус-квота", "ГБ"),
    "pause.pause_max_total_days": (1, 365, "Макс. дней паузы", "дней (1–365)"),
    "grace.grace_days": (1, 365, "Grace-дней", "дней (1–365)"),
    "app.scheduler.monitor_minutes": (1, 1440, "Частота опроса", "мин (1–1440)"),
    "app.monitoring.alert_streak": (1, 100, "Порог стрика", "замеров (1–100)"),
    "app.monitoring.service_failure_alert_minutes": (1, 1440, "Порог простоя", "мин (1–1440)"),
    "app.scheduler.backup_day": (1, 28, "День автобэкапа", "число месяца (1–28)"),
    "app.scheduler.backup_hour": (0, 23, "Час автобэкапа", "час (0–23)"),
}


def settings_prompt(key: str) -> str:
    lo, hi, label, unit = SETTINGS_BOUNDS[key]
    return f"Введи новое значение: <b>{_e(label)}</b>\nЕдиница: {_e(unit)}\nДиапазон: {lo}–{hi}."


def settings_bad_value(key: str) -> str:
    lo, hi, label, unit = SETTINGS_BOUNDS[key]
    return f"⚠️ Нужно целое число в диапазоне {lo}–{hi} ({_e(unit)}). Попробуй ещё раз."
