"""common.py — часть пакета client (см. __init__): общие кусочки экранов пользователя и гостя."""

from __future__ import annotations

import datetime

from awgbot.util import timeutil
from awgbot.core.enums import SubStatus
from ..fmt import deep_link, unit_for, amount, volume, owner_link
from ..routing import ROUTING_NAME


# Одна формулировка риска «без VPN не будет Telegram» — отдельной строкой с ⚠️:
# блокировка, удаление единственного устройства, пауза; хвост по месту
# («, и снять паузу будет нечем») дописывает экран
TELEGRAM_RISK = "⚠️ Если Telegram у тебя открывается только через этот VPN — бот станет недоступен"

# payload deep-link'ов клиента и гостя (/start <payload>): экран реестра
SUB_PAYLOAD = "sub"
RF_PAYLOAD_CLIENT = "rf"
DEV_PAYLOAD = "dev"


# ─────────────────────────────────────────────────────────────────────────────
# Срок подписки (блок админской карточки — как есть)
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Общие кусочки экранов клиента и гостя
# ─────────────────────────────────────────────────────────────────────────────

def _link(bot_username: str, payload: str, label: str) -> str:
    return deep_link(bot_username, payload, label) if bot_username else label


def vpn_status_line(server_ok: bool) -> str:
    """Первая строка главной пользователя и гостя — про сервер, не про доступ
    человека: его состояние — отдельной строкой ниже (access_status_line),
    чтобы свои проблемы отличались от проблем сервера."""
    return "🟢 Сервер доступен" if server_ok else "🔴 Сервер не отвечает"


def rf_status_short(routing_ok, routing_on: bool, bot_username: str = "") -> str:
    """«🇷🇺 РФ-доступ 🟢» / «… 🔴 недоступен» / «… выкл» — простым текстом.
    routing_ok=None — функция профилю не выдана: пусто."""
    if routing_ok is None:
        return ""
    label = f"🇷🇺 {ROUTING_NAME}"
    if not routing_on:
        return f"{label} выкл"
    return f"{label} 🟢" if routing_ok else f"{label} 🔴 недоступен"


def status_line(server_ok: bool, routing_ok, routing_on: bool, bot_username: str = "") -> str:
    """Вторая строка главной: VPN и, если выдан, РФ-доступ."""
    rf = rf_status_short(routing_ok, routing_on, bot_username)
    return vpn_status_line(server_ok) + (f" · {rf}" if rf else "")


def _pause_until(client) -> str:
    """Дата авто-снятия паузы пользователя: с начала паузы + зарезервированные дни."""
    if not client.pause_active_since:
        return ""
    until = (timeutil.parse_iso(client.pause_active_since)
             + datetime.timedelta(days=int(client.pause_reserved_days)))
    return timeutil.fmt_date_ui(until)


def subscription_short(client, bot_username: str = "") -> str:
    """Строка подписки на главной пользователя — вся ссылкой на «💳 Подписка», одно
    начало во всех ветках: «💳 Подписка до 12.10», «💳 Подписка 🟡 истекает
    12.10 18:00», «💳 Подписка ⏸️ на паузе до 01.10», «💳 Подписка ⏸️ на паузе
    (поставил администратор)», «💳 Подписка 🔴 истекла», «💳 Подписка
    бессрочная». Год — только не текущий."""
    _, mode, pause_visible = _pause_visibility(client)
    if pause_visible:
        if mode == "user":
            until = _pause_until(client)
            text = f"⏸️ на паузе до {until}" if until else "⏸️ на паузе"
        else:
            text = ADMIN_PAUSE_STATUS
    elif client.status != SubStatus.ACTIVE or (
            client.period_end and timeutil.remaining_seconds(timeutil.parse_iso(client.period_end)) <= 0):
        end = timeutil.parse_iso(client.period_end) if client.period_end else None
        text = f"🔴 истекла {timeutil.fmt_date_ui(end)}" if end else "🔴 истекла"
    elif not client.effective_period_end:
        text = "бессрочная"
    else:
        # срок — эффективный: тихая пауза администратора обнуляет period_end,
        # а выдавать себя не должна (экран «💳 Подписка» считает так же)
        end = timeutil.parse_iso(client.effective_period_end)
        if client.notified_thresholds:
            text = f"🟡 истекает {timeutil.fmt_end_ui(end)}"
        else:
            text = f"до {timeutil.fmt_date_ui(end)}"
    return _link(bot_username, SUB_PAYLOAD, f"💳 Подписка {text}")


# статус паузы, которую поставил администратор, — на главной, в «💳 Подписка»
# и у гостя о владельце (канон «пауза, не приостановка»)
ADMIN_PAUSE_STATUS = "⏸️ на паузе (поставил администратор)"


def traffic_short(rx: int, tx: int, limit_bytes: int, bonus_bytes: int = 0) -> str:
    """«📊 12.3 из 100 ГБ», с бонусом «📊 12.3 из 100(+50) ГБ», от терабайта —
    в ТБ; без лимита — «📊 12.3 ГБ (безлимит)», и ноль тоже: «📊 0 ГБ (безлимит)»."""
    total = int(rx or 0) + int(tx or 0)
    if limit_bytes:
        unit, ub = unit_for(total, limit_bytes, bonus_bytes)
        bonus = f"(+{amount(bonus_bytes, ub)})" if bonus_bytes else ""
        return f"📊 {amount(total, ub)} из {amount(limit_bytes, ub)}{bonus} {unit}"
    return f"📊 {volume(total)} (безлимит)"


def held_devices_tail(held) -> str:
    """Хвост «(+1 от профиля Вася)» к счётчику устройств пользователя, который
    держит чужие; пусто — не держит."""
    if not held:
        return ""
    return f" (+{len(held)} от профиля {owner_link(held[0])})"


def devices_short(slots, held=()) -> str:
    """Строка устройств на главной — одна форма счётчика, как в заголовке
    «📱 Устройства»: «📱 Устройства: 2 из 3 (+1 от профиля Вася)»; без лимита —
    «📱 Устройства: 2»; пока нет — «📱 Устройств пока нет · можно добавить до 3»."""
    used, limit = slots
    tail = held_devices_tail(held)
    if used == 0 and not tail:
        if limit == 0:
            return "📱 Устройств пока нет"
        return f"📱 Устройств пока нет · можно добавить до {limit}"
    if limit == 0:
        return f"📱 Устройства: {used}{tail}"
    return f"📱 Устройства: {used} из {limit}{tail}"


def limit_exhausted_line(used: int, limit: int) -> str:
    """«Лимит исчерпан: чтобы добавить новое, удали N», N = занято − лимит + 1;
    пусто, пока место есть."""
    if not limit or used < limit:
        return ""
    n = used - limit + 1
    return f"Лимит исчерпан: чтобы добавить новое, удали {n}"


def _pause_visibility(client, *, for_admin: bool = False) -> tuple:
    from awgbot.core import blocks
    mask = int(client.block_reason)
    paused = bool(mask & int(blocks.ClientBlock.PAUSED))
    mode = client.pause_mode or ""
    silent_admin = bool(mask & int(blocks.ClientBlock.ADMIN_SILENT)) and \
        not bool(mask & int(blocks.ClientBlock.ADMIN_NOTIFIED))
    pause_visible = paused and (for_admin or mode == "user" or not silent_admin)
    return paused, mode, pause_visible
