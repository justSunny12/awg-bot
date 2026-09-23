"""Базовые форматтеры: экранирование, объёмы, устройства, ссылки на людей, склонения."""

from __future__ import annotations

import html
from awgbot.util import timeutil


def deep_link(bot_username: str, payload: str, label: str) -> str:
    """Кликабельный текст в сообщении бота — только ссылка. Deep-link на самого
    себя: нажатие шлёт «/start <payload>», бот команду удаляет и открывает экран.
    Без username — просто текст."""
    if not bot_username:
        return _e(label)
    return f'<a href="https://t.me/{bot_username}?start={payload}">{_e(label)}</a>'


def _e(s) -> str:
    """Экранирование пользовательских строк (имён) для HTML parse_mode."""
    return html.escape(str(s))


# ─────────────────────────────────────────────────────────────────────────────
# Единицы
# ─────────────────────────────────────────────────────────────────────────────

def _num(value, unit_bytes: int) -> str:
    """Число в единицах unit_bytes: арифметическое округление до сотых,
    незначащие нули долой — «8.99», «50», «0.5», «0»."""
    from decimal import Decimal, ROUND_HALF_UP
    d = (Decimal(int(value or 0)) / Decimal(unit_bytes)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)
    s = f"{d:f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def gb(num_bytes: int) -> str:
    """Гигабайты числом, без единицы. Одна шкала на все экраны: ноль — «0»,
    любая ненулевая мелочь — «0.01», дальше — до сотых."""
    n = int(num_bytes or 0)
    if 0 < n < _BYTES_PER_GB // 100:
        return "0.01"
    return _num(n, _BYTES_PER_GB)


def human_bytes(n: int) -> str:
    """Объём с единицей — всегда в ГБ: «0 ГБ», «0.01 ГБ», «8.99 ГБ»."""
    return f"{gb(n)} ГБ"


def used_of_limit(used: int, limit_bytes: int, note: str = "") -> str:
    """«8.99 из 50 ГБ» (единица одна — не повторяем); без лимита — «8.99 ГБ».
    note — чей лимит, в скобках: «… (лимит устройства)»; лимит профиля — без
    пометки."""
    if not limit_bytes:
        return human_bytes(used)
    return f"{gb(used)} из {gb(limit_bytes)} ГБ" + (f" ({note})" if note else "")


def _updown(rx: int, tx: int) -> str:
    return f"(↑ {human_bytes(rx)} | ↓ {human_bytes(tx)})"


# ─────────────────────────────────────────────────────────────────────────────
# Потребление (в UI слово «трафик» заменено на «потребление», чтобы не пугать).
# Расход — автоформатом human_bytes; лимит — в ГБ с 2 знаками. Клиент/друг видят
# СУММУ (up+down) без разбивки; админ — тотал + разбивку ↑↓.
# ─────────────────────────────────────────────────────────────────────────────

# Дубль services.BYTES_PER_GB — НАМЕРЕННО: физическая константа (разойтись не
# может), а импорт services сюда тащил бы весь сервис-слой в текст-слой.
_BYTES_PER_GB = 1024 ** 3


def gb_str(num_bytes: int) -> str:
    """Лимит с единицей: «100 ГБ», «0.5 ГБ». 0 трактуется вызывающим как безлимит."""
    return f"{gb(num_bytes)} ГБ"


def _dev_traffic_line(dev_limit_bytes: int, profile_limit_bytes: int, *, own: bool) -> str:
    """Строка о лимите потребления устройства для отчёта о создании.
    Свой лимит устройства → показываем его; иначе потребление ограничено лишь
    лимитом профиля («твоего», когда устройство создано для друга — лимит
    остаётся у дарителя). Когда не ограничивает ни то, ни другое — говорить не
    о чем, и оговорка про лимит профиля только сбивает: лимита нет вовсе."""
    if dev_limit_bytes:
        return f"Потребление устройства: {gb_str(dev_limit_bytes)}"
    if profile_limit_bytes:
        whose = "лимита профиля" if own else "твоего лимита профиля"
        return f"Потребление устройства не ограничено в рамках {whose}"
    return "Потребление устройства не ограничено"


def _limit_devices_str(limit: int) -> str:
    return "∞" if not limit else str(limit)


def _device_count_line(device_count: int, max_devices: int) -> str:
    if not max_devices:
        return f"Количество устройств: {device_count}"
    return f"Количество устройств: {device_count}/{max_devices}"


def device_created_report(dev_name: str, *, client_name: str = None,
                          device_count: int = 0, max_devices: int = 0,
                          dev_limit_bytes: int = 0, profile_limit_bytes: int = 0,
                          for_friend: bool = False) -> str:
    """Отчёт о создании устройства: имя, потребление, счётчик. client_name —
    только для админа (у клиента один профиль); for_friend — «создано для
    друга», лимит — «твоего» профиля."""
    head = f"✅ Устройство «{_e(dev_name)}» создано"
    if client_name:
        head += f" для профиля «{_e(client_name)}»"
    elif for_friend:
        head += " для друга"
    return (head + ".\n"
            + _dev_traffic_line(dev_limit_bytes, profile_limit_bytes, own=not for_friend) + ".\n"
            + _device_count_line(device_count, max_devices))


def consumption_line(used_sum: int, limit_bytes: int, *, blocked: bool) -> str:
    """Строка потребления устройства у клиента/друга: «8.99 из 50 ГБ (лимит
    устройства)», без своего лимита — «8.99 ГБ». blocked — лимит исчерпан;
    когда снимется, говорит строка блокировок ниже, не эта."""
    line = f"Потребление за месяц: {used_of_limit(used_sum, limit_bytes, 'лимит устройства')}"
    if blocked and limit_bytes:
        line += " — исчерпан"
    return line


def consumption_line_admin(rx: int, tx: int, limit_bytes: int) -> str:
    """Строка потребления устройства для админа: то же + разбивка ↑↓."""
    return (f"Потребление: {used_of_limit(int(rx) + int(tx), limit_bytes, 'лимит устройства')} "
            f"{_updown(rx, tx)}")


def client_total_line(rx: int, tx: int, limit_bytes: int, bonus_bytes: int,
                      *, for_admin: bool) -> str:
    """Тотал клиента. С доп.квотой показываем разбивку «лимит + доп. до конца
    месяца» и клиенту, и админу (по договорённости — не словом «бонус»)."""
    total = int(rx) + int(tx)
    if limit_bytes and bonus_bytes:
        base = f"{gb(total)} из {gb(limit_bytes)} + {gb(bonus_bytes)} ГБ до конца месяца"
    else:
        base = used_of_limit(total, limit_bytes)
    if for_admin:
        return f"Потребление профиля за месяц: {base} {_updown(rx, tx)}"
    return f"Потребление за месяц: {base}"


# ─────────────────────────────────────────────────────────────────────────────
# Устройства
# ─────────────────────────────────────────────────────────────────────────────

def device_label(dev, *, for_admin: bool = False) -> str:
    """Имя устройства + звёздочка «создано не ботом» + индикатор онлайна +
    маркер блокировки.
    Маркер 🛑 показывается по ВИДИМОЙ для роли маске: тихий админ-блок пользователю
    не виден (устройство выглядит рабочим). Суффикс/маркер в имени не хранятся."""
    from awgbot.core import blocks
    online = timeutil.handshake_is_online(dev.last_handshake)
    dot = "🟢" if online else "🔴"
    name = _e(dev.name)
    if not dev.is_managed:
        name = f"{name} <b>*</b>"
    marker = blocks.blocked_marker_device(int(dev.block_reason), for_admin=for_admin)
    if getattr(dev, "is_gateway", 0):
        return f"{dot} 🛰 {name}"
    return f"{marker}{dot} {name}"


def plain_ip(addr: str) -> str:
    """Адрес моноширинным: внутри <code> Telegram не делает автоссылку из
    IP, а тап по нему копирует адрес в буфер — ровно то, что с адресом и
    делают. Невидимые соединители были хуже: копировались вместе с адресом."""
    return f"<code>{_e(str(addr or ''))}</code>"


def device_line(dev) -> str:
    """Строка устройства для списка: индикатор, имя (IP), последний коннект."""
    last = timeutil.fmt_handshake(dev.last_handshake)
    return f"{device_label(dev)} ({plain_ip(dev.address)}), последний коннект: {last}"


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
        parts.append(consumption_line(used, dev.traffic_limit, blocked=traffic_blocked))
    reasons = blocks.device_reasons_ru(mask, for_admin=for_admin)
    if reasons:
        parts.append("⛔ Заблокировано: " + ", ".join(reasons))
    return "\n".join(parts)


# ── Ссылки на людей ─────────────────────────────────────────────────────────

def tg_link(name: str, tg_id, username: str = "") -> str:
    """Имя человека ссылкой на его Telegram-аккаунт. По username — t.me/…,
    её видят все; ссылка tg://user?id= у постороннего (не в контактах, нет
    общих чатов) молча превращается в текст, поэтому она — запасная. Без
    tg_id — просто имя."""
    label = _e(name or "профиль")
    if username:
        return f'<a href="https://t.me/{_e(username.lstrip("@"))}">{label}</a>'
    if tg_id:
        return f'<a href="tg://user?id={int(tg_id)}">{label}</a>'
    return label


def client_link(c) -> str:
    """Ссылка на человека: имя его Telegram-аккаунта (профильное name — про
    подписку, его задаёт админ), пока имени нет — профильное."""
    return tg_link(getattr(c, "tg_name", "") or c.name, c.tg_id, getattr(c, "tg_username", ""))


def owner_name(dev) -> str:
    """Как звать владельца устройства: имя его Telegram-аккаунта, пока нет —
    профильное. Для кнопок (там ссылка невозможна) и для owner_link."""
    return dev.owner_tg_name or dev.owner_name


def holder_name(dev) -> str:
    return dev.holder_tg_name or dev.holder_name


def owner_link(dev) -> str:
    return tg_link(owner_name(dev), dev.owner_tg_id, dev.owner_tg_username)


def holder_link(dev) -> str:
    return tg_link(holder_name(dev), dev.holder_tg_id, dev.holder_tg_username)


def client_label(c, *, bold: bool = True) -> str:
    """Профиль в списках объявления: имя профиля (его дал админ), а если у
    аккаунта своё имя — оно ссылкой в скобках: «Ксюша ([Ксения])». В подсказке
    и превью имя жирным, в отчёте — обычным."""
    head = f"<b>{_e(c.name)}</b>" if bold else _e(c.name)
    tg = getattr(c, "tg_name", "") or ""
    if tg and tg != c.name:
        return f"{head} ({client_link(c)})"
    return head


def _n_devices(n: int) -> str:
    return f"{n} {plural_ru(n, 'устройство', 'устройства', 'устройств')}"


# ── Склонения, возраст данных, иконка устройства ──────────────────────────────

def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение по числу — переиспользует хелпер из timeutil
    (единая логика на весь проект). 1 устройство, 2 устройства, 5 устройств."""
    return timeutil._plural_ru(n, (one, few, many))


def _days_word(n: int) -> str:
    return plural_ru(n, "день", "дня", "дней")


def _days(n: int) -> str:
    return f"{n} {plural_ru(n, 'день', 'дня', 'дней')}"


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


def device_emoji(d) -> str:
    """Иконка типа устройства — единая для текстов и кнопок: 🛰 шлюз,
    ⏳ отдано другу, но инвайт ещё не принят, 📲 у друга, 📱 своё."""
    if getattr(d, "is_gateway", 0):
        return "🛰"
    if getattr(d, "is_lent", False):
        return "📲"
    if d.friend is not None and d.friend.status == "pending":
        return "⏳"
    return "📱"
