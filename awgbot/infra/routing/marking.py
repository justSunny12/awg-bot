"""marking.py — плечо хоста — цепочка маркировки, ip rule, маршруты линка и локальных подсетей."""

from __future__ import annotations

from typing import Optional

from awgbot.core import config

from . import base
from .base import log

_MARK_HEX = f"0x{config.ROUTING_FWMARK:x}"


# ─────────────────────────────────────────────────────────────────────────────
# Плечо ХОСТА: цепочка маркировки
# ─────────────────────────────────────────────────────────────────────────────

_MARK = f"{_MARK_HEX}/{_MARK_HEX}"


def _mangle(args: list[str], **kw):
    return base._host(["iptables", "-t", "mangle", *args], **kw)


def rebuild_chain(client_ids) -> None:
    """Пересобрать цепочку маркировки целиком: флаш и наполнение заново.

    Своя цепочка вместо вставок в PREROUTING — потому что правила пер-клиентские
    и появляются/исчезают. С `-C || -I` пришлось бы вычислять разницу и удалять
    осиротевшие правила удалённых клиентов; флаш своей цепочки делает это даром.

    Правило на клиента — без отрицаний: «источник его И назначение в ЕГО наборе»
    → на шлюз. Набор у каждого свой и уже содержит и базовый список, и личные
    добавления (пер-юзерный merge, см. domain/routing.build_dnsmasq_conf),
    поэтому пересечение двух наборов проверять не нужно.

    Метим то, что В НАБОРЕ: на шлюз идут только перечисленные там российские
    сервисы, всё прочее выходит зарубежным адресом обычным путём.

    Обратная модель («метим то, чего в наборе нет») существовала и упразднена:
    через домашний канал шёл почти весь трафик по объёму. Отсюда же следует, что
    пустой набор безопасен — он равносилен выключенной функции, а не аварии.
    """
    chain = config.ROUTING_CHAIN
    desired = [(base.src_set(cid), base.user_set(cid)) for cid in sorted(client_ids)]
    # дифф перед записью: один `-S` вместо флаша и правила на профиль. Если
    # цепочка уже ровно такая — не трогаем: флаш оставлял окно без маркировки
    # 480 раз в сутки ради того же самого набора правил.
    if chain_rules() == desired:
        return
    _mangle(["-N", chain], check=False)          # уже есть → код 1, это норма
    _mangle(["-F", chain])
    for src, dst in desired:
        _mangle(["-A", chain,
                 "-m", "set", "--match-set", src, "src",
                 "-m", "set", "--match-set", dst, "dst",
                 "-j", "MARK", "--set-xmark", _MARK])
    # Хук в PREROUTING ставит НЕ эта функция, а set_marking_enabled: именно
    # наличие хука и есть рубильник, и цепочка вполне может быть собрана и
    # лежать без дела — так выглядит деградация. Сам хук сужен клиентской
    # подсетью: до цепочки доходит только немаскараженный трафик включённых
    # устройств, остальному в ней делать нечего.


def parse_chain_rules(text: str, chain: str) -> Optional[list[tuple[str, str]]]:
    """Правила цепочки маркировки из `iptables -t mangle -S <chain>` как
    [(src-набор, dst-набор)] в порядке цепочки. Разбираем СТРУКТУРНО: пары
    `--match-set <имя> <src|dst>` и цель MARK с нашей меткой, а не текст —
    iptables-nft печатает опции в своём порядке. Любое правило чужой формы →
    None: тогда цепочку пересобираем, это безопаснее, чем счесть её верной."""
    rules: list[tuple[str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] in ("-N", "-P"):
            continue
        if parts[:2] != ["-A", chain]:
            return None
        src = dst = None
        i = 0
        while i < len(parts):
            if parts[i] == "--match-set" and i + 2 < len(parts):
                if parts[i + 2] == "src":
                    src = parts[i + 1]
                elif parts[i + 2] == "dst":
                    dst = parts[i + 1]
                i += 3
                continue
            i += 1
        if "MARK" not in parts or "--set-xmark" not in parts:
            return None
        mark = parts[parts.index("--set-xmark") + 1]
        if mark.lower() != _MARK.lower() or src is None or dst is None:
            return None
        rules.append((src, dst))
    return rules


def chain_rules() -> Optional[list[tuple[str, str]]]:
    proc = _mangle(["-S", config.ROUTING_CHAIN], check=False)
    if proc.returncode != 0:
        return None                                   # цепочки нет
    return parse_chain_rules(proc.stdout.decode(errors="replace"), config.ROUTING_CHAIN)


def _hooks() -> list[list[str]]:
    """Хуки в PREROUTING — ПО ОДНОМУ НА КЛИЕНТСКУЮ ПОДСЕТЬ.

    Функция, а не константа: на время переезда профилей подсетей две, и хук,
    суженный одной, оставлял бы вторую вообще без маркировки. Отказ при этом
    молчаливый и обманчивый — туннель работает, DNS отвечает, интернет есть,
    а российские сервисы видят зарубежный адрес. Ровно то, ради чего функция и
    существует, не работает, и связать это с переездом неоткуда.

    Константа тут была бы вычислена на импорте, до того как конфиг переезда
    прочитан, — поэтому именно функция.
    """
    return [["-s", subnet, "-j", config.ROUTING_CHAIN]
            for subnet, _iface in config.routing_client_subnets()]
# С МАСКОЙ, как и сама маркировка. Без маски правило требует точного равенства
# метки, и любой чужой бит в fwmark (docker, tc, сторонний firewall) увёл бы
# трафик мимо таблицы. Метим мы маскированно (--set-xmark 0x1/0x1) — сверять
# обязаны так же, иначе две половины механизма расходятся в трактовке метки.
_RULE = ["fwmark", _MARK, "lookup", str(config.ROUTING_TABLE)]


def _hook_present() -> bool:
    """Все ли хуки на месте. Частично поставленный набор — это «не поставлен»:
    подсеть без хука не метится, а рубильник показывал бы включённое состояние."""
    return all(base._host_ok(["iptables", "-t", "mangle", "-C", "PREROUTING", *hook])
               for hook in _hooks())


def _rule_present() -> bool:
    proc = base._host(["ip", "rule", "show"], check=False, timeout=base._PROBE_TIMEOUT)
    text = proc.stdout.decode(errors="replace")
    return _MARK_HEX in text and f"lookup {config.ROUTING_TABLE}" in text


def ensure_route(iface: str = "") -> None:
    """Маршрут по умолчанию в таблице фичи — в линк АКТИВНОГО шлюза."""
    base._host(["ip", "route", "replace", "default", "dev", iface or base._active_if(),
           "table", str(config.ROUTING_TABLE)])


def switch_active(iface: str) -> None:
    """Переложить клиентский трафик на другой линк — это и есть переключение
    шлюза: одна команда, маркировка не снимается ни на такт."""
    ensure_route(iface)
    log.info("routing: трафик РФ-доступа — через %s", iface)


def ensure_home_routes(pairs=None) -> None:
    """Домашние подсети за шлюзом — в ЕГО линк, в ОСНОВНОЙ таблице: без маршрута
    пакет устройства админа на 192.168.x.x ушёл бы в интернет ВПС и умер.
    Кому туда можно, решает файервол шлюза (устройствам админа), здесь только
    путь. pairs — [(подсеть, интерфейс)]; по умолчанию — из конфига в линк
    конфига. Убранная подсеть остаётся в ядре до ребута — осознанно."""
    import ipaddress
    if pairs is None:
        pairs = [(n, base._active_if()) for n in config.ROUTING_HOME_SUBNETS]
    for net, iface in pairs:
        try:
            ipaddress.ip_network(net, strict=False)
        except ValueError:
            log.warning("routing.home_subnets: %r не похоже на подсеть — пропущено", net)
            continue
        base._host(["ip", "route", "replace", net, "dev", iface])


def drop_home_routes(pairs) -> None:
    """Снять маршруты убранных подсетей: [(подсеть, интерфейс)]. Нет маршрута —
    не ошибка (ip route del на отсутствующем даёт код 2)."""
    for net, iface in pairs:
        base._host(["ip", "route", "del", net, "dev", iface], check=False)
