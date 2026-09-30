"""
routing/ — единственный слой команд условной маршрутизации. Части пакета:

  base       журнал, замок перестроек, ошибки, запуск команд на хосте и в контейнере, имена наборов, интерфейс активного линка
  selfcheck  самопроверка обвязки: инструменты хоста, статика, подсети, кэш вердикта с бэкоффом
  slots      слоты шлюзов: таблица и бит метки слота, политика слота
  sets       плечо контейнера — исключения из MASQUERADE; наборы ipset
  marking    плечо хоста — цепочка маркировки, ip rule, маршруты линка и локальных подсетей
  policy     наблюдение за состоянием и сборка политики: правило, маршрут таблицы, MSS-клэмп, включение маркировки
  probes     живость линка: адрес и состояние пира, зонды, пинг, возраст рукопожатия
  feeds      внешние списки и конфиг dnsmasq: загрузка, резолв, добавление сетей, запись конфига


Отношение к domain/routing.py то же, что у awg.py к configgen.py: там чистые
преобразования, здесь всё, что дёргает систему.

ДВА ПЛЕЧА — И ТОЛЬКО В DOCKER-РЕЖИМЕ. Контейнер Amnezia работает в режиме
bridge, то есть в собственном сетевом namespace, а ipset — сущность per-netns:
набор, созданный внутри контейнера, для dnsmasq на хосте просто не существует и
наполняться не будет. При этом различать клиентов можно только ДО MASQUERADE
контейнера — за ним все пиры выглядят одним bridge-адресом (ровно поэтому там же
живут блокировки, см. awg.block_ip). Отсюда разделение:

  • в КОНТЕЙНЕРЕ — только исключения из MASQUERADE: по одному правилу на
    включённое устройство. Ничего, кроме iptables, там не требуется;
  • на ХОСТЕ — наборы ipset, маркировка, ip rule, dnsmasq и линк до шлюза.

Работает это потому, что немаскараженный трафик приходит на хост с настоящим
10.8.1.x, а весь остальной — с bridge-адреса контейнера. Различение достаётся
даром, без передачи метки между namespace (она бы и не пережила переход).

Весь этот механизм — плата за чужой netns, и в host-режиме (config.AWG_RUNTIME,
docs/ROADMAP.md) он не нужен: MASQUERADE ровно один, наш, клиенты приходят на
mangle PREROUTING с настоящими адресами, отменять нечего. Поэтому контейнерное
плечо там выключается целиком, а не имитируется.

СТАТИКА И ДИНАМИКА. Бот управляет только тем, что зависит от состояния: исключения,
наборы, цепочку маркировки, ip rule. Базовый обвяз хоста — MASQUERADE для
клиентской подсети, маршрут обратно в контейнер, разрешения в FORWARD — ставится
один раз при развёртывании и ботом не трогается: менять на горячую базовый NAT
боевого сервера ради фичи неоправданно. Наличие обвяза проверяет self_check().

ДЕГРАДАЦИЯ. Всё, что фича делает с маршрутизацией, снимается одним движением:
убрали ip rule — помеченный трафик пошёл обычным путём. Поэтому недоступность
шлюза не ломает клиентам интернет, а лишь возвращает им зарубежный адрес.
"""

from __future__ import annotations

from . import base, selfcheck, slots, sets, marking, policy, probes, feeds

_PARTS = (base, selfcheck, slots, sets, marking, policy, probes, feeds)


def __getattr__(name: str):
    """Любое имя пакета — из его частей, живьём: подмена на определяющей части
    (base._host, marking._MARK) видна и через routing.X, и через from-import."""
    for part in _PARTS:
        if name in vars(part):
            return vars(part)[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    names = set(globals())
    for part in _PARTS:
        names.update(n for n in vars(part) if not n.startswith('__'))
    return sorted(names)


__all__ = [
    "RoutingError", "RoutingUnavailable", "mutation_lock",
    # самопроверка
    "self_check", "available", "invalidate_self_check",
    # имена наборов
    "user_set", "src_set",
    # плечо контейнера
    "sync_nat_exempt",
    # наборы
    "ensure_set", "replace_members", "add_networks", "destroy_set", "list_sets",
    # маркировка и политика
    "rebuild_chain", "set_marking_enabled", "link_handshake_age", "link_peer_state",
    "probe_gateway", "link_peer_address", "resolve_a",
    "PROBE_OK", "PROBE_NO_PATH", "PROBE_DOWN",
    "ensure_mss_clamp", "mss_clamp_present",
    "rule_present", "table_route", "set_count", "hook_present", "ensure_policy",
    "probe_source", "last_probe_latency_ms",
    # внешние списки и dnsmasq
    "fetch", "write_dnsmasq_conf",
]
