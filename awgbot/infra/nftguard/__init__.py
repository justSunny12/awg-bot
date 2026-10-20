"""
nftguard/ — файервол ВПС таблицей nftables `inet awg_bot_guard` (части: common —
константы, спецификация и ошибка; collect — вайтлист, порты, подсети и линки с
живого хоста и conf; render — текст таблицы; apply — применение, чтение, сверка
по тику, персистентность, откат и снятие).

Пакет — ЕДИНСТВЕННАЯ точка файервола хоста бота: nft-таблица
`inet awg_bot_guard`, которую целиком ведёт бот.

До этого на пути SSH-пакета из туннеля стояло четверо ворот (nft-таблица
harden'а, цепочка iptables AWGBOT_SSH, PostUp-страж в conf интерфейса, sshd
Match Address) плюс ufw рядом, и отказ на любых выглядел одинаково. Теперь
ворота одни:

  • статика (порт SSH, внешний вайтлист, порты интерфейсов, подсети туннеля,
    политика INPUT/FORWARD) — из conf/app.yaml (`firewall.*`, `network.*`);
  • динамика (адреса устройств админа, которым открыт SSH из туннеля) — set
    `admin4`, бот правит его по событию и сверяет каждый тик; им же открыт
    путь к другим пирам туннеля (шлюз, устройства друг друга) — прочим пирам
    друг до друга пути нет; им же — адреса в туннеле локальных сетей шлюзов
    (routing.lan_space), прочим пирам туда drop;
  • частные адреса назначения из туннеля клиентов и линков во внешний
    интерфейс не уходят (и в NAT-only форме) — адрес в туннеле без таблицы
    awg_lan_map и старая закладка на настоящий адрес сети шлюза не уходят к
    провайдеру;
  • всё вместе лежит в /etc/nftables.d/awg-bot-guard.nft, который грузит
    nftables.service ДО подъёма интерфейсов — fail-closed с загрузки, без
    PostUp-строк и без окна «интерфейс поднялся, бот ещё не реассертил».

NAT клиентов — тоже здесь (host-режим): цепочка postrouting маскарадит
подсети туннеля во всё, что не awg-интерфейс. Раньше его давал обвяз условной
маршрутизации, и чистый хост без него оставался без интернета для клиентов.
Поэтому таблица существует ВСЕГДА:
при firewall.enabled=false — в «NAT-only» форме (наборы и postrouting, без
фильтрующих цепочек), при включённом файерволе — целиком. «Выключить
файервол» снимает фильтр, но не NAT.

Применение — всегда атомарная замена таблицы (`table X; delete table X;
table X {…}` одним `nft -f`): нет момента с полупустой цепочкой. Сверка по
тику дешёвая: текст желаемой таблицы сравнивается с файлом (без exec), живой
set — одним `nft -j list set`.

Хост видит пиров с настоящими адресами → пер-пирный SSH (set устройств
админа) и своя цепочка FORWARD.
"""
from __future__ import annotations

from .common import log, TABLE_FAMILY, TABLE_NAME, TABLE, RULES_DIR, RULES_FILE, MAIN_CONF, ROLLBACK_UNIT, ROLLBACK_SECONDS, SET_ALLOW4, SET_ALLOW6, SET_TUNNEL_NETS, SET_TUNNEL_ADMIN, SET_LINK_PEERS, PRIVATE4, GuardError, GuardSpec, enabled
from .collect import _resolve_cache, _RESOLVE_TTL, _RESOLVE_TIMEOUT, _getaddrinfo_timed, classify, _resolve, resolve_allow, _ports_from_conf, listen_ports, tunnel_nets, build_spec, _peer_link_ifs, _tunnel_ifs, wan_ifaces, link_ifaces, link_peer_addrs, link_channel_port, _nat_exclude_ifs, _is_v4, quarantine_ifaces
from .render import _set_block, _ports, render
from .apply import _nft, read_file, write_file, check_text, apply_text, nat_covers, table_present, live_set, reconcile, ensure_persistence, rollback_armed, arm_rollback, disarm_rollback, remove, ufw_active, firewalld_active, status

__all__ = ["log", "TABLE_FAMILY", "TABLE_NAME", "TABLE", "RULES_DIR", "RULES_FILE", "MAIN_CONF", "ROLLBACK_UNIT", "ROLLBACK_SECONDS", "SET_ALLOW4", "SET_ALLOW6", "SET_TUNNEL_NETS", "SET_TUNNEL_ADMIN", "SET_LINK_PEERS", "PRIVATE4", "GuardError", "GuardSpec", "enabled", "_resolve_cache", "_RESOLVE_TTL", "_RESOLVE_TIMEOUT", "_getaddrinfo_timed", "classify", "_resolve", "resolve_allow", "_ports_from_conf", "listen_ports", "tunnel_nets", "build_spec", "_peer_link_ifs", "_tunnel_ifs", "wan_ifaces", "link_ifaces", "link_peer_addrs", "link_channel_port", "_nat_exclude_ifs", "_is_v4", "quarantine_ifaces", "_set_block", "_ports", "render", "_nft", "read_file", "write_file", "check_text", "apply_text", "nat_covers", "table_present", "live_set", "reconcile", "ensure_persistence", "rollback_armed", "arm_rollback", "disarm_rollback", "remove", "ufw_active", "firewalld_active", "status"]
