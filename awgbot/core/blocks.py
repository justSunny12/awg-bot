"""
blocks.py — причины блокировки как БИТОВЫЕ МАСКИ (IntFlag).

Зачем маска, а не одно значение: причин блокировки может быть НЕСКОЛЬКО
одновременно (истекла подписка И превышен трафик И ручной блок админа). Одиночное
поле их бы перетирало — сняв одну причину, мы потеряли бы вторую и разблокировали
то, что должно оставаться заблокированным. Маска хранит все причины сразу
(побитовое ИЛИ), а блок снимается физически лишь когда сброшены ВСЕ биты.

В БД лежит просто целое число (сумма активных битов). Единственный источник
истины «заблокирован ли» — `block_reason != 0`; отдельного is_blocked нет.

Два ОТДЕЛЬНЫХ enum (устройство и клиент) — намеренно: причины у них
семантически разные и будут расходиться при развитии функционала, поэтому
масштабируются независимо. Общие числовые значения — совпадение, не связь; не
полагаться на равенство DeviceBlock.X == ClientBlock.X.

ВИДИМОСТЬ: тихий админ-блок (ADMIN_SILENT) пользователю не показывается — для
него устройство/клиент выглядит незаблокированным. Админ видит все причины.
Разворачивание маски в текст — reasons_ru(mask, for_admin).
"""
from __future__ import annotations

from enum import IntFlag


class DeviceBlock(IntFlag):
    """Причины блокировки УСТРОЙСТВА (маска в devices.block_reason).
    Порядок битов ЕДИН с ClientBlock — каскады «клиент→устройство» переносят бит
    по значению (int), полагаясь на совпадение позиций. Поэтому при вставке нового
    бита сдвигаем ОБА enum одинаково (см. TRAFFIC_CLIENT ниже)."""
    NONE = 0
    EXPIRY = 1          # подписка клиента-владельца истекла (авто)
    TRAFFIC_USER = 2    # превышен лимит потребления САМОГО устройства (авто)
    TRAFFIC_CLIENT = 4  # каскад: исчерпан ТОТАЛ-лимит клиента-владельца (авто).
                        # Отдельный бит от TRAFFIC_USER (а не общий) — иначе снятие
                        # одной причины сняло бы блокировку и по другой (админ
                        # поднял лимит устройству → случайно разлочил каскад клиента).
    ADMIN_SILENT = 8    # ручной блок админом БЕЗ уведомления (пользователю не виден)
    ADMIN_NOTIFIED = 16 # ручной блок админом С уведомлением (виден пользователю)
    USER = 32           # ручной блок устройства самим клиентом (всегда виден)
    PAUSED = 64         # каскад приостановки клиента (самоблок «в отпуск»)


class ClientBlock(IntFlag):
    """Причины блокировки КЛИЕНТА (маска в clients.block_reason).
    Порядок битов ЕДИН с DeviceBlock (см. там). Отдельный enum — своя семантика.
    Трафик-биты названы как у устройства для симметрии: TRAFFIC_USER (bit2) у
    клиента ПУСТ (причины «свой лимит устройства» у клиента нет), рабочий —
    TRAFFIC_CLIENT (bit4), он же каскадит на устройства тем же битом по значению."""
    NONE = 0
    EXPIRY = 1          # подписка истекла (авто)
    # RESERVED: TRAFFIC_USER=2 — у клиента логики нет (это device-only причина);
    #           держим для выравнивания позиций с DeviceBlock.
    TRAFFIC_USER = 2
    TRAFFIC_CLIENT = 4  # РАБОЧИЙ: превышен тотал-лимит после исчерпания доп.квоты;
                        # он же — источник каскада DeviceBlock.TRAFFIC_CLIENT.
    ADMIN_SILENT = 8    # ручной блок админом БЕЗ уведомления (клиенту не виден)
    ADMIN_NOTIFIED = 16 # ручной блок админом С уведомлением (клиенту виден)
    # RESERVED: USER=32 — задел под будущее (клиент-USER пока НИГДЕ не ставится).
    USER = 32
    PAUSED = 64         # приостановка подписки самим клиентом («в отпуск», тикание стоит)


# Ручные биты (снимаются руками через UI). EXPIRY/TRAFFIC — авто, тут их нет.
# PAUSED тоже не тут: приостановка снимается своим потоком (выход из паузы с
# пересчётом периода), а не обычной кнопкой «разблокировать».
DEVICE_MANUAL = DeviceBlock.ADMIN_SILENT | DeviceBlock.ADMIN_NOTIFIED | DeviceBlock.USER
CLIENT_MANUAL = ClientBlock.ADMIN_SILENT | ClientBlock.ADMIN_NOTIFIED
# Админские биты (каскадируются с клиента на устройства, снимает только админ).
DEVICE_ADMIN = DeviceBlock.ADMIN_SILENT | DeviceBlock.ADMIN_NOTIFIED
CLIENT_ADMIN = ClientBlock.ADMIN_SILENT | ClientBlock.ADMIN_NOTIFIED
# Любая трафик-причина устройства: своя ИЛИ каскад клиента — для проверок
# «заблокировано по трафику» в UI и логике (там, где неважно, чей именно лимит).
DEVICE_TRAFFIC_ANY = DeviceBlock.TRAFFIC_USER | DeviceBlock.TRAFFIC_CLIENT

# Человекочитаемые названия причин (для UI). Ключи — конкретные биты.
_DEVICE_REASON_RU = {
    DeviceBlock.EXPIRY: "подписка истекла",
    DeviceBlock.TRAFFIC_USER: "исчерпан лимит устройства",
    DeviceBlock.TRAFFIC_CLIENT: "исчерпан лимит профиля",
    DeviceBlock.ADMIN_SILENT: "заблокировано администратором (тихо)",
    DeviceBlock.ADMIN_NOTIFIED: "заблокировано администратором",
    DeviceBlock.USER: "заблокировано владельцем",
    DeviceBlock.PAUSED: "подписка приостановлена",
}
_CLIENT_REASON_RU = {
    ClientBlock.EXPIRY: "подписка истекла",
    ClientBlock.TRAFFIC_CLIENT: "исчерпан лимит потребления",
    ClientBlock.ADMIN_SILENT: "заблокирован администратором (тихо)",
    ClientBlock.ADMIN_NOTIFIED: "заблокирован администратором",
    ClientBlock.USER: "заблокирован владельцем",
    ClientBlock.PAUSED: "приостановлено пользователем",
}

# Что скрывать от пользователя (клиент/друг) — тихий админ-блок.
_HIDDEN_FROM_USER_DEVICE = DeviceBlock.ADMIN_SILENT
_HIDDEN_FROM_USER_CLIENT = ClientBlock.ADMIN_SILENT


def has(mask: int, bit: IntFlag) -> bool:
    """Активен ли конкретный бит в маске."""
    return bool(int(mask) & int(bit))


def add(mask: int, bit: IntFlag) -> int:
    """Вернуть маску с установленным битом (побитовое ИЛИ)."""
    return int(mask) | int(bit)


def clear(mask: int, bit: IntFlag) -> int:
    """Вернуть маску со снятым битом."""
    return int(mask) & ~int(bit)


def visible_to_user_device(mask: int) -> int:
    """Маска устройства без скрытых от пользователя причин (тихий админ-блок)."""
    return int(mask) & ~int(_HIDDEN_FROM_USER_DEVICE)


def visible_to_user_client(mask: int) -> int:
    """Маска клиента без скрытых от пользователя причин."""
    return int(mask) & ~int(_HIDDEN_FROM_USER_CLIENT)


def device_reasons_ru(mask: int, *, for_admin: bool) -> list[str]:
    """Читаемые причины блокировки устройства. Пользователю тихий блок не виден."""
    m = int(mask) if for_admin else visible_to_user_device(mask)
    return [txt for bit, txt in _DEVICE_REASON_RU.items() if has(m, bit)]


def client_reasons_ru(mask: int, *, for_admin: bool) -> list[str]:
    """Читаемые причины блокировки клиента. Пользователю тихий блок не виден."""
    m = int(mask) if for_admin else visible_to_user_client(mask)
    return [txt for bit, txt in _CLIENT_REASON_RU.items() if has(m, bit)]


def blocked_marker_device(mask: int, *, for_admin: bool) -> str:
    """Маркер 🛑 для списков, если есть ВИДИМАЯ причина блокировки (иначе '')."""
    m = int(mask) if for_admin else visible_to_user_device(mask)
    return "🛑 " if m != 0 else ""


def blocked_marker_client(mask: int, *, for_admin: bool) -> str:
    m = int(mask) if for_admin else visible_to_user_client(mask)
    return "🛑 " if m != 0 else ""


__all__ = ["DeviceBlock", "ClientBlock", "DEVICE_MANUAL", "CLIENT_MANUAL",
           "DEVICE_ADMIN", "CLIENT_ADMIN", "DEVICE_TRAFFIC_ANY", "has", "add", "clear",
           "visible_to_user_device", "visible_to_user_client",
           "device_reasons_ru", "client_reasons_ru",
           "blocked_marker_device", "blocked_marker_client"]
