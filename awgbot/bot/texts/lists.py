"""lists.py — тексты экранов «весь список» (bot/lists): шапки видов, пустые
состояния, снятый слот, переезд вне окна."""
from __future__ import annotations

from .fmt import _e, plural_ru

SLOT_GONE = "🛰 Выбранный шлюз снят"
MIGRATION_NOT_RUNNING = "🚚 Переезд сейчас не идёт"


def gwnets_title(name: str) -> str:
    return f"🗺 Подсети {_e(name)}"


def gwnets_empty(name: str) -> str:
    return (f"🗺 <b>Подсети {_e(name)}</b>\n\n"
            "Подсети не заданы — без них сеть шлюза из туннеля не открыть")


def gwpeers_title(name: str) -> str:
    return f"🔗 Связь подсетей {_e(name)}"


def gateways_count(n: int) -> str:
    return f"{n} {plural_ru(n, 'шлюз', 'шлюза', 'шлюзов')}"


def gwpeers_empty(name: str) -> str:
    return (f"🔗 <b>Связь подсетей {_e(name)}</b>\n\n"
            "Связи нет: VPN-транзит включён меньше чем у двух шлюзов")


PEERNETS_TITLE = "🔗 Связь подсетей"
PEERNETS_OFF = ("🔗 <b>Связь подсетей</b>\n\n"
                "Выключена вместе с адресами в туннеле — см. карточку шлюза")
PEERNETS_EMPTY = ("🔗 <b>Связь подсетей</b>\n\n"
                  "Связь подсетей появится, когда VPN-транзит будет включён хотя бы у двух шлюзов")

FWUNRES_TITLE = "🛡 Не резолвятся"
UNRES_EMPTY = "✅ Все имена из списка SSH-доступа резолвятся"


def drift_title(name: str) -> str:
    return f"🛰 Конфигурация {_e(name)}"


def drift_count(n: int) -> str:
    return f"{n} {plural_ru(n, 'расхождение', 'расхождения', 'расхождений')}"


def drift_empty(name: str) -> str:
    return f"✅ Конфигурация {_e(name)} актуальна — расхождений сейчас нет"


MIGDROP_TITLE = "🚚 Потеряют связь при завершении"
MIGDROP_EMPTY = "✅ Все недавно активные устройства уже на новом интерфейсе"

SSHFROM_TITLE = "🛡 SSH изнутри"
SSHFROM_LAN = "Из локальной сети"
SSHFROM_PEERS = "Из подсетей других шлюзов"


def subnets_count(n: int) -> str:
    return f"{n} {plural_ru(n, 'подсеть', 'подсети', 'подсетей')}"


SSHFROM_EMPTY = ("🛡 <b>SSH изнутри</b>\n\n"
                 "Подсетей локальной сети и других шлюзов обвязка шлюза не прислала")
SSHUNRES_TITLE = "🛡 Не резолвятся"
SSHUNRES_HELD = "Держу прошлые адреса"
TUNADDR_TITLE = "🔀 Адреса в туннеле"
TUNADDR_EMPTY = "ℹ️ Адресов в туннеле нет — сервер AWG их не прислал"

