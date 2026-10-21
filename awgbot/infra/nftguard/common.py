"""common.py — часть пакета nftguard (см. __init__)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from awgbot.core import settings


log = logging.getLogger("awgbot.nftguard")

TABLE_FAMILY = "inet"
TABLE_NAME = "awg_bot_guard"
TABLE = f"{TABLE_FAMILY} {TABLE_NAME}"
RULES_DIR = "/etc/nftables.d"
RULES_FILE = f"{RULES_DIR}/awg-bot-guard.nft"
MAIN_CONF = "/etc/nftables.conf"
ROLLBACK_UNIT = "awg-bot-fw-rollback"
ROLLBACK_SECONDS = 180

SET_ALLOW4 = "ssh_allow4"
SET_ALLOW6 = "ssh_allow6"
SET_TUNNEL_NETS = "tunnel_nets4"
SET_TUNNEL_ADMIN = "admin4"
# Адреса шлюзов в /30 линков: им и только им открыт порт канала.
# Клиентская подсеть в tunnel_nets4 сюда не входит — канал не для клиентов.
SET_LINK_PEERS = "link_peers4"
# Пул адресов Telegram: только с них порт вебхука гостевой зоны резерва уходит
# DNAT в зону; ведёт бот резерва (ядро — диапазоны со страницы Bot API).
SET_TG_POOL = "tg_pool4"
SET_TG_SEEN = "tg_seen4"           # SYN на порт вебхука вне пула: адрес → число за 10 мин (обучение пула)
DEFAULT_TG_POOL = ("149.154.160.0/20", "91.108.4.0/22")
# Частные адреса (RFC 1918): из туннеля к провайдеру они не уходят. Служебное
# пространство адресов в туннеле (10.240.0.0/12, routing.lan_space) — внутри
# них по построению: util/lanspace принимает только частную /12.
PRIVATE4 = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")


class GuardError(RuntimeError):
    """nft/файл/резолв не удались — вызывающий решает, логировать или падать."""


# ── спецификация таблицы ─────────────────────────────────────────────────────

@dataclass
class GuardSpec:
    ssh_port: int
    ssh_allow4: list[str] = field(default_factory=list)   # IP/CIDR v4 (вайтлист)
    ssh_allow6: list[str] = field(default_factory=list)
    ssh_open: bool = False            # вайтлист пуст → SSH открыт всем (осознанно)
    tunnel_nets4: list[str] = field(default_factory=list)  # подсети туннеля
    tunnel_admin4: list[str] = field(default_factory=list) # устройства админа
    tunnel_ifs: list[str] = field(default_factory=list)    # awg-интерфейсы клиентов
    udp_ports: list[int] = field(default_factory=list)     # порты интерфейсов awg
    open_tcp: list[int] = field(default_factory=list)      # прочие порты хоста
    open_udp: list[int] = field(default_factory=list)
    own_forward: bool = True          # своя цепочка FORWARD (policy drop)
    unresolved: list[str] = field(default_factory=list)    # имена, которые не резолвятся
    nat: bool = True                  # MASQUERADE подсетей туннеля наружу
    nat_exclude_ifs: list[str] = field(default_factory=list)  # куда НЕ маскарадить: awg-интерфейсы, линк
    filter: bool = True               # False — NAT-only форма (файервол выключен)
    peer_link_ifs: list[str] = field(default_factory=list)    # линки слотов с VPN-транзитом: между ними
                                                              # открыт транзит (при двух и больше)
    peer_link_block: list[str] = field(default_factory=list)  # NAT-only форма: все линки (при двух и
                                                              # больше) — транзит между ними закрыт,
                                                              # кроме пар из peer_link_ifs
    link_peers4: list[str] = field(default_factory=list)      # адреса шлюзов в /30 линков
    link_ifs: list[str] = field(default_factory=list)         # интерфейсы линков — вход канала только с них
    link_channel_port: int = 0                                # порт канала; 0 — канала нет
    link_channel_extra: list[int] = field(default_factory=list)   # прежний порт канала после смены — неделю открыт тоже
    admin_dns4: str = ""                                      # адрес резолвера админа (host): :53 — только admin4
    lan_space4: str = ""                                      # пространство адресов в туннеле (host): туда —
                                                              # только устройствам админа
    wan_ifs: list[str] = field(default_factory=list)          # внешние интерфейсы (host): частным адресам
                                                              # назначения туда хода нет
    quarantine_ifs: list[str] = field(default_factory=list)   # линки сопряжения (штамп в конфиге): только
                                                              # канал на адрес ВПС, пересылки нет
    quarantine_pairs: list[tuple] = field(default_factory=list)   # (линк, адрес ВПС, адрес шлюза) — канал
                                                                  # каждого линка сопряжения — только своей паре
    pair_if: str = ""                                         # линк пары серверов (резерв): вход — только канал
    pair_peer4: str = ""                                      #   пары с адреса второй стороны в /30, пересылки нет
    pair_port: int = 0                                        #   порт канала пары (TCP внутри линка)
    # гостевая зона резерва (клиенты основы у меня): состояние зоны из state.json
    guest_phase: str = ""                                     # idle | taking | serving | relay
    guest_udp_port: int = 0                                   # порт клона: сокет у хоста — вход открыт (taking, serving)
    guest_udp_ports: list[int] = field(default_factory=list)  # все клоны (второй — окно переезда профилей)
    guest_veth_if: str = ""                                   # veth хост ↔ зона: выход зоны наружу через NAT хоста
    guest_zone4: str = ""                                     # адрес зоны в veth: DNAT вебхука сюда
    guest_zone_net4: str = ""                                 # /30 veth: маскарад наружу
    guest_webhook_port: int = 0                               # порт вебхука: DNAT в зону только с пула Telegram (serving)
    tg_pool4: list[str] = field(default_factory=list)         # пул адресов Telegram (набор tg_pool4)
    guest_rf_if: str = ""                                     # veth РФ-трафика зоны (хост): в линки слотов, маскарад
    guest_rf_zone4: str = ""                                  # адрес зоны в нём — источник РФ-трафика
    relay_to: str = ""                                        # «адрес:порт» основы: пересылка хвоста UDP (relay)
    relay_udp_port: int = 0                                   # порт, который пересылается
    relay_udp_ports: list[int] = field(default_factory=list)  # все порты клонов — каждый на тот же порт основы


def enabled() -> bool:
    return settings.get_bool("app.firewall.enabled", False)

