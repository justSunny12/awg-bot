"""policy.py — наблюдение за состоянием и сборка политики: правило, маршрут таблицы, MSS-клэмп, включение маркировки."""

from __future__ import annotations

from typing import Optional

from awgbot.core import config

from . import base, marking
from .base import log

# ── Наблюдение за состоянием (только чтение; для диагностики) ────────────────

def rule_present() -> bool:
    """Стоит ли ip rule — постоянная обвязка, не рубильник."""
    return marking._rule_present()


def hook_present() -> bool:
    """Стоит ли РУБИЛЬНИК: прыгает ли PREROUTING в цепочку маркировки."""
    return marking._hook_present()


def table_route() -> Optional[str]:
    """Маршрут по умолчанию в таблице фичи, как его показывает ядро."""
    proc = base._host(["ip", "route", "show", "default", "table",
                  str(config.ROUTING_TABLE)], check=False, timeout=base._PROBE_TIMEOUT)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode(errors="replace").strip() or None


def mss_clamp_present(iface: str = "") -> bool:
    iface = iface or base._active_if()
    if not iface:
        return False
    return base._host_ok(["iptables", "-t", "mangle", "-C", "FORWARD",
                     "-o", iface, "-p", "tcp",
                     "--tcp-flags", "SYN,RST", "SYN",
                     "-j", "TCPMSS", "--clamp-mss-to-pmtu"])


def set_count(name: str) -> int:
    """Сколько записей в наборе — для диагностики.

    Пустой набор не авария (правило метит то, что В НАБОРЕ, значит из пустого
    следует «на шлюз не идёт ничего»), но означает, что режим не работает: либо
    списки не скачаны, либо dnsmasq не может писать в ipset — а это самый
    молчаливый из отказов, резолв при нём исправен."""
    proc = base._host(["ipset", "list", name, "-t"], check=False, timeout=base._PROBE_TIMEOUT)
    if proc.returncode != 0:
        return 0
    for line in proc.stdout.decode(errors="replace").splitlines():
        if "Number of entries" in line:
            try:
                return int(line.split(":")[1].strip())
            except (IndexError, ValueError):
                return 0
    return 0


def ensure_mss_clamp(iface: str = "") -> None:
    """Подрезать MSS у соединений, уходящих на шлюз. Идемпотентно.

    Путь на шлюз инкапсулирован ДВАЖДЫ: клиент → ВПС (AmneziaWG) и ВПС → шлюз
    (второй туннель со своей обфускацией). Заголовков набегает заметно больше,
    чем на обычном пути, а клиент об этом не знает — он согласовал MSS под MTU
    своего туннеля. Крупные сегменты упираются в лимит уже за ВПС.

    Само по себе это лечится PMTU Discovery, но он держится на ICMP «fragmentation
    needed», который должен пройти обратно через оба туннеля и домашний
    провайдер. На практике эти ICMP теряются, и получается худший вид поломки:
    хендшейк и DNS проходят (пакеты мелкие), а страницы не открываются — со
    стороны «интернет отвалился», хотя связь есть.

    Поэтому не надеемся на ICMP, а подрезаем MSS в SYN. Правило висит в mangle
    FORWARD, потому что TCPMSS в PREROUTING не действует, а маркировка живёт
    именно там.
    """
    iface = iface or base._active_if()
    if not iface:
        return
    rule = ["-o", iface, "-p", "tcp",
            "--tcp-flags", "SYN,RST", "SYN",
            "-j", "TCPMSS", "--clamp-mss-to-pmtu"]
    if not base._host_ok(["iptables", "-t", "mangle", "-C", "FORWARD", *rule]):
        base._host(["iptables", "-t", "mangle", "-A", "FORWARD", *rule])
        log.info("routing: включён MSS-кламп на %s", iface)


def ensure_policy(active_iface: str = "", slots=()) -> None:
    """Статическая часть политики: маршрут, ip rule и MSS-кламп. Идемпотентно.

    Раньше правило и маршрут ставились вместе с включением режима и снимались
    вместе с ним. Из-за этого зонд живости не мог проверить путь в состоянии
    деградации: маршрут в шлюз лежит в таблице фичи, а выбирает её как раз это
    правило — снят рубильник, и проверять стало нечем, то есть вернуться из
    деградации бот мог только вслепую.

    Поэтому маршрут с правилом — постоянная обвязка (сами по себе они трафик
    никуда не уводят: в цепочку никто не прыгает, метить некому), а рубильником
    служит ХУК в PREROUTING.

    active_iface — линк, который несёт трафик; slots — [(slot_id, iface,
    home_subnets)] всех слотов: у каждого своя таблица для зонда, MSS-кламп на
    своём линке и маршруты в свои домашние подсети. Без слотов (до первого
    назначения) — как раньше, по интерфейсу конфига.
    """
    active_iface = active_iface or base._active_if()
    if not active_iface:
        return
    marking.ensure_route(active_iface)
    if slots:
        pairs = [(net, iface) for _sid, iface, nets in slots for net in nets]
        marking.ensure_home_routes(pairs)
        for sid, iface, _nets in slots:
            slots.ensure_slot_policy(sid, iface)
            ensure_mss_clamp(iface)
    else:
        marking.ensure_home_routes()
        ensure_mss_clamp(active_iface)
    if not marking._rule_present():
        base._host(["ip", "rule", "add", *marking._RULE])


def set_marking_enabled(on: bool) -> None:
    """Главный рубильник: прыгает ли PREROUTING в цепочку маркировки.

    Выключение — механизм ГРАЦИОЗНОЙ ДЕГРАДАЦИИ при непроходимом шлюзе: метить
    перестаём, трафик уходит обычным путём с зарубежным адресом. Пользователь
    видит «банк ругается на IP», а не «интернета нет». Идемпотентно.
    """
    if not config.ROUTING_GW_INTERFACE:
        return
    hooks = marking._hooks()
    if on:
        missing = [h for h in hooks
                   if not base._host_ok(["iptables", "-t", "mangle", "-C", "PREROUTING", *h])]
        if missing:
            ensure_policy()
            for hook in missing:
                marking._mangle(["-I", "PREROUTING", *hook])
            log.info("routing: маркировка включена (%d подсет.)", len(hooks))
        return
    removed = False
    for hook in hooks:
        # Снимаем ВСЕ копии: дубли хука мог оставить прошлый запуск, и одного
        # -D тогда мало — режим остался бы включённым при снятом рубильнике.
        while base._host_ok(["iptables", "-t", "mangle", "-C", "PREROUTING", *hook]):
            marking._mangle(["-D", "PREROUTING", *hook], check=False)
            removed = True
    if removed:
        log.info("routing: маркировка снята (деградация)")
