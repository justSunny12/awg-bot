"""Базовые форматтеры: экранирование, ссылки на экраны, дерево вложенных строк, объёмы, устройства, ссылки на людей, склонения."""

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


def profile_link(client, bot_username: str) -> str:
    """Имя профиля — ссылка на его карточку у админа (/start cl-<id>); у
    профиля самого админа карточки нет — простым текстом."""
    from awgbot.core import config
    if client is None:
        return "?"
    if getattr(client, "tg_id", 0) == config.ADMIN_ID:
        return _e(client.name)
    return deep_link(bot_username, f"cl-{int(client.id)}", client.name)


def admin_device_link(dev, bot_username: str) -> str:
    """Имя устройства — ссылка на его карточку у админа (/start dev-<id>)."""
    return deep_link(bot_username, f"dev-{int(dev.id)}", dev.name)


# ── вложенные строки ─────────────────────────────────────────────────────────
# Экраны-списки — плоские: записи через пустую строку, вложенная строка под
# записью — со знаком включения «└ » (то же в карточках и на главной).
_SUB = "└ "


def tree(rows) -> str:
    """rows — [(строка, [вложенные строки])]: записи через пустую строку,
    вложенные — с «└ » сразу под своей записью."""
    return "\n\n".join("\n".join([line] + [_SUB + x for x in subs]) for line, subs in rows)


def sub_line(text: str) -> str:
    """Одна вложенная строка под записью («└ 🇷🇺 РФ-доступ: …»)."""
    return _SUB + text


def access_status_line(client) -> str:
    """Строка состояния доступа над строкой подписки — у админа в карточке
    профиля, у клиента на главной и в «💳 Подписка»: истечение и пауза —
    «🟡 доступ приостановлен», исчерпанный лимит трафика (после доп. квоты) —
    «🟡 исчерпан лимит трафика за месяц»; ручная блокировка — не здесь."""
    from awgbot.core import blocks
    from awgbot.core.enums import SubStatus
    mask = int(getattr(client, "block_reason", 0) or 0)
    if mask & int(blocks.ClientBlock.TRAFFIC_CLIENT):
        return "🟡 исчерпан лимит трафика за месяц"
    if mask & int(blocks.ClientBlock.PAUSED) or getattr(client, "status", None) != SubStatus.ACTIVE:
        return "🟡 доступ приостановлен"
    return ""


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


_BYTES_PER_TB = 1024 ** 4


def unit_for(*values: int) -> tuple[str, int]:
    """Единица по большему из значений: от терабайта — «ТБ», иначе «ГБ»; в
    паре «использовано из лимита» единица одна на оба числа."""
    if max((int(v or 0) for v in values), default=0) >= _BYTES_PER_TB:
        return "ТБ", _BYTES_PER_TB
    return "ГБ", _BYTES_PER_GB


def amount(num_bytes: int, unit_bytes: int) -> str:
    """Число в единице без подписи: ноль — «0», ненулевая мелочь — «0.01»."""
    n = int(num_bytes or 0)
    if 0 < n < unit_bytes // 100:
        return "0.01"
    return _num(n, unit_bytes)


def volume(num_bytes: int) -> str:
    """Объём с единицей: «0 ГБ», «0.01 ГБ», «8.99 ГБ», «1.5 ТБ»."""
    unit, ub = unit_for(num_bytes)
    return f"{amount(num_bytes, ub)} {unit}"


def human_bytes(n: int) -> str:
    """Объём с единицей (см. volume)."""
    return volume(n)


def used_of_limit(used: int, limit_bytes: int, note: str = "") -> str:
    """«8.99 из 50 ГБ» (единица одна — не повторяем, от терабайта — «ТБ»);
    без лимита — «8.99 ГБ». note — чей лимит, в скобках: «… (лимит
    устройства)»; лимит профиля — без пометки."""
    if not limit_bytes:
        return volume(used)
    unit, ub = unit_for(used, limit_bytes)
    return f"{amount(used, ub)} из {amount(limit_bytes, ub)} {unit}" + (f" ({note})" if note else "")


def _updown(rx: int, tx: int) -> str:
    return f"(↑ {human_bytes(rx)} | ↓ {human_bytes(tx)})"


def rf_value(rx: int, tx: int, *, arrows: bool = True) -> str:
    """«7.1 ГБ (↑0.7 ↓6.4)» — объём РФ-части; без стрелок — только сумма."""
    total = human_bytes(int(rx) + int(tx))
    return f"{total} {updown_brief(rx, tx)}" if arrows else total


def updown_brief(rx: int, tx: int) -> str:
    """«(↑1.2 ↓11.1)» — разбивка без единиц: единица уже стоит у суммы."""
    return f"(↑{gb(rx)} ↓{gb(tx)})"


def rf_line(rx: int, tx: int, label: str = "", *, arrows: bool = True) -> str:
    """Вложенная строка РФ-части под строкой трафика (карточки, главная):
    «└ 🇷🇺 РФ-доступ: 7.1 ГБ (↑0.7 ↓6.4)». label — готовая подпись вместо
    «🇷🇺 РФ-доступ» (ссылкой на экран трафика; флаг — часть ссылки)."""
    from .routing import ROUTING_NAME
    return sub_line(f"{label or f'🇷🇺 {ROUTING_NAME}'}: {rf_value(rx, tx, arrows=arrows)}")


# ─────────────────────────────────────────────────────────────────────────────
# Трафик. Расход — автоформатом human_bytes; лимит — в ГБ с 2 знаками.
# Клиент/друг видят СУММУ (up+down) без разбивки; админ — тотал + разбивку ↑↓.
# ─────────────────────────────────────────────────────────────────────────────

# Дубль services.BYTES_PER_GB — НАМЕРЕННО: физическая константа (разойтись не
# может), а импорт services сюда тащил бы весь сервис-слой в текст-слой.
_BYTES_PER_GB = 1024 ** 3


def gb_str(num_bytes: int) -> str:
    """Лимит с единицей: «100 ГБ», «0.5 ГБ», «2 ТБ». 0 трактуется вызывающим как безлимит."""
    return volume(num_bytes)


def _limit_devices_str(limit: int) -> str:
    return "∞" if not limit else str(limit)


# ─────────────────────────────────────────────────────────────────────────────
# Устройства
# ─────────────────────────────────────────────────────────────────────────────

def device_state(dev, *, for_admin: bool = False) -> str:
    """Один значок состояния устройства, по приоритету: ⛔ заблокировано
    (видимой для роли причиной) → ⏳ приглашение другу не принято → 🟢 онлайн
    → ⚪ офлайн. Для строк списков и кнопок."""
    from awgbot.core import blocks
    if getattr(dev, "is_gateway", 0):
        return "🛰"
    if blocks.blocked_marker_device(int(getattr(dev, "block_reason", 0)), for_admin=for_admin):
        return "⛔"
    friend = getattr(dev, "friend", None)
    if friend is not None and getattr(friend, "status", None) == "pending":
        return "⏳"
    return "🟢" if timeutil.handshake_is_online(getattr(dev, "last_handshake", None)) else "⚪"


def details(text: str) -> str:
    """Свёрнутый абзац «подробнее»: раскрывается нажатием, экран остаётся
    коротким."""
    return f"<blockquote expandable>{text}</blockquote>"


def plain_ip(addr: str) -> str:
    """Адрес моноширинным: внутри <code> Telegram не делает автоссылку из
    IP, а тап по нему копирует адрес в буфер — ровно то, что с адресом и
    делают. Невидимые соединители были хуже: копировались вместе с адресом."""
    return f"<code>{_e(str(addr or ''))}</code>"


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


def _n_devices(n: int) -> str:
    return f"{n} {plural_ru(n, 'устройство', 'устройства', 'устройств')}"


# ── Склонения, возраст данных, иконка устройства ──────────────────────────────

def num(n) -> str:
    """«1 234 567» — разряды пробелом; одно место на все счётчики."""
    return f"{int(n or 0):,}".replace(",", " ")


def more(items, shown: int = 12, code: bool = True) -> str:
    """Список через запятую, не длиннее shown — «и ещё N»: адреса и домены
    моноширинным (code), имена — текстом. Кнопки — до 8, текст — до 12:
    лимит 4096 при длинных именах."""
    items = list(items)
    out = ", ".join((f"<code>{_e(x)}</code>" if code else _e(x)) for x in items[:shown])
    if len(items) > shown:
        out += f" и ещё {len(items) - shown}"
    return out


def unlimited(used: int) -> str:
    """«3.2 ГБ (безлимит)» — и ноль тоже: «0 ГБ (безлимит)»."""
    return f"{volume(used)} (безлимит)"


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение по числу — переиспользует хелпер из timeutil
    (единая логика на весь проект). 1 устройство, 2 устройства, 5 устройств."""
    return timeutil._plural_ru(n, (one, few, many))


def _days_word(n: int) -> str:
    return plural_ru(n, "день", "дня", "дней")


def _days(n: int) -> str:
    return f"{n} {plural_ru(n, 'день', 'дня', 'дней')}"


