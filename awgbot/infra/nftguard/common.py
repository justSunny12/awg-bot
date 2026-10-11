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


class GuardError(RuntimeError):
    """nft/файл/резолв не удались — вызывающий решает, логировать или падать."""


# ── спецификация таблицы ─────────────────────────────────────────────────────

@dataclass
class GuardSpec:
    ssh_port: int
    ssh_allow4: list[str] = field(default_factory=list)   # IP/CIDR v4 (вайтлист)
    ssh_allow6: list[str] = field(default_factory=list)
    ssh_open: bool = False            # вайтлист пуст → SSH открыт всем (осознанно)
    tunnel_nets4: list[str] = field(default_factory=list)  # подсети туннеля (host) / bridge (docker)
    tunnel_admin4: list[str] = field(default_factory=list) # устройства админа (host)
    tunnel_ifs: list[str] = field(default_factory=list)    # awg-интерфейсы клиентов (host)
    per_peer: bool = True             # host: SSH из туннеля только устройствам админа
    udp_ports: list[int] = field(default_factory=list)     # порты интерфейсов awg
    open_tcp: list[int] = field(default_factory=list)      # прочие порты хоста
    open_udp: list[int] = field(default_factory=list)
    own_forward: bool = True          # host: своя цепочка FORWARD (policy drop)
    unresolved: list[str] = field(default_factory=list)    # имена, которые не резолвятся
    nat: bool = True                  # host: MASQUERADE подсетей туннеля наружу
    nat_exclude_ifs: list[str] = field(default_factory=list)  # куда НЕ маскарадить: awg-интерфейсы, линк
    filter: bool = True               # False — NAT-only форма (файервол выключен)
    peer_link_ifs: list[str] = field(default_factory=list)    # линки, между которыми открыт транзит
                                                              #
    peer_link_block: list[str] = field(default_factory=list)  # NAT-only форма: линки, между которыми
                                                              # транзит ЗАКРЫТ (тумблер выключен)
    link_peers4: list[str] = field(default_factory=list)      # адреса шлюзов в /30 линков
    link_ifs: list[str] = field(default_factory=list)         # интерфейсы линков — вход канала только с них
    link_channel_port: int = 0                                # порт канала; 0 — канала нет
    link_channel_extra: list[int] = field(default_factory=list)   # прежний порт канала после смены — неделю открыт тоже
    admin_dns4: str = ""                                      # адрес резолвера админа (host): :53 — только admin4


def enabled() -> bool:
    return settings.get_bool("app.firewall.enabled", False)

