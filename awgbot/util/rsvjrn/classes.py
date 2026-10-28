"""
classes.py — часть пакета rsvjrn (см. __init__): реестр классов данных базы.

У каждой таблицы и колонки — ровно один класс; новая таблица или колонка
без класса роняет тест-сторож. Класс отвечает на вопрос «чьё это и пишет ли
это следующий»:

  решение   пишет только держатель эпохи (профили, устройства, сроки, паузы,
            квоты, приглашения, ключи server_state из белого списка реплики)
  маска     решение, побитное (block_reason профиля и устройства)
  секрет    решение, которое от основы к резерву не едет (закрытый ключ)
  счётчик   измерение: пишет каждая сторона (трафик, рукопожатие)
  история   журнал изменений: дописка вместе с решением, чистка по сроку — своя
  хост      данные хоста основы (слоты шлюзов)
  местное   только этого хоста (сэмплы опроса, пара, меню, служебное)

Запрет у следующего стоит на решениях, масках и секретах; история
пишется только в одной транзакции с решением (отказ решения откатывает и
её), чистка по сроку следующему разрешена.
"""
from __future__ import annotations

from awgbot.util.dbnames import HISTORY_TABLES
from awgbot.util.rsvpkg.bot import GUEST_STATE

DECISION = "решение"
MASK = "маска"
SECRET = "секрет"
COUNTER = "счётчик"
HISTORY = "история"
HOST = "хост"
LOCAL = "местное"

DENIED = (DECISION, MASK, SECRET)

# таблица → класс всех колонок; у смешанных — {колонка: класс}
TABLES: dict[str, str | dict[str, str]] = {
    "clients": {"block_reason": MASK, "*": DECISION},
    "client_subscription": DECISION,
    "client_quota": DECISION,
    "client_grace": DECISION,
    "client_pause": DECISION,
    "devices": {"block_reason": MASK, "private_key": SECRET, "*": DECISION},
    "device_traffic": {"traffic_limit": DECISION, "missing_count": LOCAL, "*": COUNTER},
    "device_friend": DECISION,
    "client_routing_domains": DECISION,
    "migration_cohort": DECISION,
    "traffic_samples": LOCAL,
    "rf_samples": LOCAL,
    "server_state": LOCAL,                  # кроме ключей STATE_DECISION — см. ниже
    # меню чатов: учёт сообщений того, кто шлёт в Telegram; основа в следовании шлёт
    # админу уведомления о резерве напрямую — её учёт запретом не ломать
    "ui_state": LOCAL,
    "gateways": HOST,
    "reserve_peer": LOCAL,
    "reserve_traffic_monthly": LOCAL,
    "jrn_meta": LOCAL,
    **{t: HISTORY for t in HISTORY_TABLES},
}

# ключи server_state — решения (белый список реплики), кроме выбора слота шлюзов
# (его меняет живость шлюзов самого хоста, routing/liveness) и итога проверки ящика
# (email_check пишет запасной канал критичных алертов и предстартовая проверка — на
# каждом хосте свои): у следующего они остаются своими
_HOST_KEYS = ("routing_active_gateway", "routing_manual_hold", "email_check")
STATE_DECISION: tuple[str, ...] = tuple(k for k in GUEST_STATE if k not in _HOST_KEYS)


def class_of(table: str, column: str) -> str | None:
    """Класс колонки; None — таблицы или колонки в реестре нет."""
    spec = TABLES.get(table)
    if spec is None:
        return None
    if isinstance(spec, str):
        return spec
    return spec.get(column) or spec.get("*")


def denied_columns(table: str, columns: list[str]) -> list[str]:
    """Колонки таблицы, запись которых следующему запрещена (по порядку схемы)."""
    return [c for c in columns if class_of(table, c) in DENIED]


def unclassified(schema: dict[str, list[str]]) -> list[str]:
    """«таблица» или «таблица.колонка» без класса — для сторожа."""
    out: list[str] = []
    for table, cols in schema.items():
        if table.startswith("sqlite_"):
            continue
        if table not in TABLES:
            out.append(table)
            continue
        spec = TABLES[table]
        if isinstance(spec, dict) and "*" not in spec:
            out += [f"{table}.{c}" for c in cols if c not in spec]
    return out
