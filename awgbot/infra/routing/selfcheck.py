"""selfcheck.py — самопроверка обвязки: инструменты хоста, статика, подсети, кэш вердикта с бэкоффом."""

from __future__ import annotations

import time
from typing import Optional

from awgbot.core import config
from awgbot.infra import awg

from . import base, probes
from .base import log, RoutingError, RoutingUnavailable

# ─────────────────────────────────────────────────────────────────────────────
# Самопроверка
# ─────────────────────────────────────────────────────────────────────────────

_selfcheck_cache: Optional[tuple[bool, str]] = None
_selfcheck_at: float = 0.0

# Отрицательный вердикт живёт минуту, положительный — бессрочно. Асимметрия
# намеренная: цена ошибок разная. Ложное «не работает» молча выключает
# маршрутизацию ВСЕМ до перезапуска бота, а ложное «ок» ловится зондом живости
# в пределах его такта. Минута — компромисс с ценой самой проверки: она ходит в
# подпроцессы, а available() зовётся ещё и при отрисовке экранов.
_SELFCHECK_BAD_TTL = 60.0        # первая перепроверка отрицательного вердикта
_SELFCHECK_BAD_TTL_MAX = 300.0   # потолок бэкоффа: 60 → 120 → 240 → 300 с
_selfcheck_bad_streak = 0        # подряд отрицательных вердиктов


def _check_host_tools() -> None:
    """Инструменты есть И ядро реально умеет то, что от него потребуется.

    Одной проверки `ipset -V` мало: на контейнерной виртуализации (OpenVZ/LXC)
    бинарь отработает, а `ipset create` упадёт — модулей ip_set в ядре гостя
    нет. Молча это вылезло бы уже после того, как кому-то включили режим.
    Поэтому наборы проверяем делом: создаём пробный и тут же сносим.
    """
    for tool in ("ipset", "iptables", "ip"):
        if not base._host_ok([tool, "-V"]) and not base._host_ok([tool, "--version"]):
            raise RoutingUnavailable(f"{tool} не установлен на хосте")

    base._host(["ipset", "destroy", probes._PROBE_SET], check=False)
    probe = base._host(["ipset", "create", probes._PROBE_SET, "hash:ip", "-exist"], check=False)
    if probe.returncode != 0:
        raise RoutingUnavailable(
            "ядро не поддерживает ipset ("
            + probe.stderr.decode(errors="replace").strip()
            + ") — на контейнерной виртуализации фича неработоспособна")
    base._host(["ipset", "destroy", probes._PROBE_SET], check=False)

    # расширение xt_set: без него `-m set --match-set` не соберётся, а наборы
    # сами по себе ничего не маркируют
    if not base._host_ok(["iptables", "-m", "set", "--help"]):
        raise RoutingUnavailable(
            "iptables собран без поддержки `-m set` — маркировать по наборам нечем")

    # Пустоту базового набора здесь НЕ проверяем, хотя соблазн есть: это не
    # «можно ли», а «готово ли». Наполняет набор сам бот, и если считать пустоту
    # неработоспособностью, получится взаимоблокировка — код наполнения не
    # запустится, потому что набор пуст. Пустота сторожится там, где она опасна:
    # в reconcile_routing, перед включением маркировки.


def _check_static_plumbing() -> None:
    """Проверить обвяз, который ставится при развёртывании, а не ботом.

    Без него фича «работает» ровно до первого пакета: правила соберутся, наборы
    наполнятся, а трафик включённого устройства либо не выйдет с хоста, либо не
    вернётся в контейнер. Снаружи это выглядит как «интернет пропал у того, кому
    включили режим» — то есть худший из возможных отказов, поэтому проверяем
    явно и до того, как кого-то включат.
    """
    for subnet, iface in config.routing_client_subnets():
        _check_subnet_plumbing(subnet, iface)
    # Именно СЕРВИС, а не бинарь: пакет dnsmasq-base кладёт /usr/sbin/dnsmasq без
    # systemd-юнита, и `systemctl restart` при каждой правке списка падал бы.
    svc = config.ROUTING_DNSMASQ_SERVICE
    units = base._host(["systemctl", "list-unit-files", f"{svc}.service"], check=False)
    if f"{svc}.service" not in units.stdout.decode(errors="replace"):
        raise RoutingUnavailable(
            f"нет юнита {svc}.service (установлен только dnsmasq-base?) — "
            f"применить списки доменов будет нечем")


def _check_subnet_plumbing(subnet: str, iface: str) -> None:
    """Маршрут и MASQUERADE для ОДНОЙ клиентской подсети.

    Вынесено из _check_static_plumbing, потому что подсетей стало две: на время
    переезда профилей рядом со старым интерфейсом живёт новый со своей подсетью.
    Проверять только старую значило бы молчать ровно про тех, кто уже переехал, —
    а отказ у них тот же самый и такой же необъяснимый: «включили режим, пропал
    интернет».
    """
    # именно `route show <подсеть>`, а не `route get <адрес>`: get вернёт код 0
    # практически всегда, потому что подойдёт маршрут по умолчанию, — проверка
    # была бы ложноположительной. Нужен ЯВНЫЙ маршрут для этой подсети.
    proc = base._host(["ip", "route", "show", subnet], check=False, timeout=base._PROBE_TIMEOUT)
    route = proc.stdout.decode(errors="replace") if proc.returncode == 0 else ""
    if not route.strip():
        raise RoutingUnavailable(
            f"на хосте нет маршрута до {subnet} — обратный трафик не дойдёт "
            f"до клиентов")
    # Маршрут может СУЩЕСТВОВАТЬ и вести не туда. Проверять только наличие
    # недостаточно: именно так фича и «работала» после переезда на хост —
    # доктор был зелёным, а у клиентов не было интернета, потому что обратный
    # маршрут остался от контейнера и вёл в мёртвую docker-сеть.
    if awg.in_container():
        # Контейнер пересоздали, docker выдал ему другой IP, а маршрут остался
        # прежним. Снаружи — «у включённых пропал интернет» без единой ошибки.
        insp = base._host(["docker", "inspect", "-f",
                      "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                      config.CONTAINER], check=False)
        cont_ip = insp.stdout.decode(errors="replace").split()
        if insp.returncode == 0 and cont_ip and cont_ip[0] not in route:
            raise RoutingUnavailable(
                f"маршрут до {subnet} ведёт не на текущий адрес контейнера "
                f"({cont_ip[0]}) — контейнер пересоздавали? перезапустите "
                f"awg-bot-routing.service")
    elif f"dev {iface}" not in route:
        # На хосте подсеть обязана быть connected через свой awg-интерфейс. Любой
        # via-маршрут поверх connected перехватывает обратный трафик: наружу всё
        # уходит, ответы возвращаются и отправляются в никуда.
        raise RoutingUnavailable(
            f"маршрут до {subnet} ведёт мимо {iface} "
            f"({' '.join(route.split())[:80]}) — ответы клиентам уйдут не туда. "
            f"Снять лишний: ip route del {subnet}")
    # NAT клиентов живёт в таблице бота (nftguard), а у установок, переживших
    # прежнюю схему, — ещё и правилом iptables. Годится любое: спрашиваем не
    # «есть ли конкретное правило», а «выйдет ли трафик наружу».
    from awgbot.infra import nftguard
    nat_ok = False
    try:
        nat_ok = nftguard.nat_covers(subnet)
    except Exception as e:                                  # noqa: BLE001
        log.debug("nat_covers(%s): %s", subnet, e)
    if not nat_ok:
        nat_ok = base._host_ok(["iptables", "-t", "nat", "-C", "POSTROUTING",
                           "-s", subnet, "-j", "MASQUERADE"])
    if not nat_ok:
        raise RoutingUnavailable(
            f"на хосте нет MASQUERADE для {subnet} — трафик включённых устройств "
            f"не выйдет наружу (таблица awg_bot_guard: awg-bot firewall status)")


def self_check(force: bool = False) -> tuple[bool, str]:
    """(работоспособна ли фича, причина). Зовётся из preflight, из каждого тика
    планировщика и при отрисовке экранов, поэтому кэшируется.

    Отрицательный вердикт перепроверяется сам. Прежде кэш сбрасывался только
    там, где окружение менял САМ бот, — а меняется оно и снаружи: обновление
    ядра, перезапуск линка, ручная правка на хосте. Вердикт «интерфейс не
    найден», вынесенный на минуту простоя, держался до перезапуска бота или до
    ближайшего обновления списков, то есть до шести часов после того, как
    причина устранена.
    """
    global _selfcheck_cache, _selfcheck_at, _selfcheck_bad_streak
    if _selfcheck_cache is not None and not force:
        # Отрицательный вердикт перепроверяется с бэкоффом: сломанная обвязка
        # стоила 12–18 exec на каждый тик и каждый рендер экрана. Всё, что
        # чинит сам бот (обвяз, шлюз, списки, детект рестарта), сбрасывает кэш
        # явно; после ручной починки по SSH фича вернётся не позже чем через
        # потолок бэкоффа — либо сразу по «Доктору» (force).
        ttl = min(_SELFCHECK_BAD_TTL * 2 ** max(0, _selfcheck_bad_streak - 1),
                  _SELFCHECK_BAD_TTL_MAX)
        if _selfcheck_cache[0] or time.time() - _selfcheck_at < ttl:
            return _selfcheck_cache

    result: tuple[bool, str]
    if not config.ROUTING_ENABLED:
        result = (False, "выключена в конфиге (routing.gw_interface пуст)")
    else:
        try:
            _check_host_tools()
            if not base._host_ok(["ip", "link", "show", config.ROUTING_GW_INTERFACE]):
                raise RoutingUnavailable(
                    f"интерфейс {config.ROUTING_GW_INTERFACE} не найден на хосте")
            # Только в docker-режиме: на хосте контейнерного плеча нет вовсе, и
            # проверка «доступен ли iptables в контейнере» гасила бы фичу из-за
            # отсутствия контейнера, которого там и не должно быть.
            if awg.in_container() and not base._cont_ok(["iptables", "-t", "nat", "-L", "-n"]):
                raise RoutingUnavailable("iptables недоступен в контейнере")
            _check_static_plumbing()
            result = (True, "ок")
        except RoutingUnavailable as e:
            result = (False, str(e))
        except RoutingError as e:
            result = (False, f"проверка не удалась: {e}")

    prev, _selfcheck_cache = _selfcheck_cache, result
    _selfcheck_at = time.time()
    _selfcheck_bad_streak = 0 if result[0] else _selfcheck_bad_streak + 1
    # Логируем СМЕНУ вердикта, а не каждую проверку: пока отказ держится, он
    # переспрашивается раз в минуту, и «неактивна» сыпалось бы в журнал вечно,
    # хороня под собой ту единственную строку, где отказ начался. Возврат к «ок»
    # тоже строка — иначе в журнале отказ никогда не заканчивается.
    if prev != result:
        if not result[0]:
            log.warning("routing: фича неактивна — %s", result[1])
        elif prev is not None:
            log.info("routing: фича снова активна")
    return result


def available() -> bool:
    return self_check()[0]


def invalidate_self_check() -> None:
    """Сбросить кэш самопроверки.

    Нужен после того, как окружение изменилось по нашей же инициативе (залили
    списки, доставили обвяз): иначе закэшированное «недоступна» держалось бы до
    перезапуска бота, хотя причина уже устранена."""
    global _selfcheck_cache, _selfcheck_at, _selfcheck_bad_streak
    _selfcheck_cache = None
    _selfcheck_at = 0.0
    _selfcheck_bad_streak = 0
