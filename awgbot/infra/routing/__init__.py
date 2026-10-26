"""
routing/ — единственный слой команд условной маршрутизации. Части пакета:

  base       журнал, замок перестроек, ошибки, запуск команд на хосте, имена наборов, интерфейс активного линка
  selfcheck  самопроверка обвязки: инструменты хоста, статика, подсети, кэш вердикта с бэкоффом
  slots      слоты шлюзов: таблица и бит метки слота, политика слота
  sets       наборы ipset
  marking    цепочка маркировки, ip rule, маршрут линка
  policy     наблюдение за состоянием и сборка политики: правило, маршрут таблицы, MSS-клэмп, включение маркировки
  probes     живость линка: адрес и состояние пира, зонды, пинг, возраст рукопожатия
  feeds      внешние списки и конфиг dnsmasq: загрузка, резолв, добавление сетей, запись конфига
  lanmap     таблица inet awg_lan_map: адреса в туннеле локальных сетей шлюзов (своё
             пространство имён: routing.lanmap.render и т. п., в общий список не входит)

Отношение к domain/routing.py то же, что у infra/awg/ к configgen.py: там чистые
преобразования, здесь всё, что дёргает систему.

ВСЁ НА ХОСТЕ. MASQUERADE ровно один, наш (таблица файервола), пользователи приходят
на mangle PREROUTING с настоящими адресами: наборы ipset, маркировка, ip rule,
dnsmasq и линк до шлюза живут в одном сетевом пространстве.

СТАТИКА И ДИНАМИКА. Бот управляет только тем, что зависит от состояния:
наборы, цепочку маркировки, ip rule. Базовый обвяз хоста — MASQUERADE для
подсети пользователей, разрешения в FORWARD — ставится
один раз при развёртывании и ботом не трогается: менять на горячую базовый NAT
боевого сервера ради фичи неоправданно. Наличие обвяза проверяет self_check().

ДЕГРАДАЦИЯ. Всё, что фича делает с маршрутизацией, снимается одним движением:
убрали ip rule — помеченный трафик пошёл обычным путём. Поэтому недоступность
шлюза не ломает пользователям интернет, а лишь возвращает им зарубежный адрес.
"""

from __future__ import annotations

from . import base, selfcheck, slots, sets, marking, policy, probes, feeds

_PARTS = (base, selfcheck, slots, sets, marking, policy, probes, feeds)
from . import lanmap  # noqa: E402 — по имени модуля: render/apply/present не для общего списка


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
    # адреса в туннеле локальных сетей шлюзов — модулем
    "lanmap",
]
