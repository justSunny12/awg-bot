"""
routing.py — единственный слой команд условной маршрутизации.

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

import logging
import os
import ipaddress
import socket
import subprocess
import time
import threading
from typing import Optional

from awgbot.core import config
from awgbot.infra import awg

log = logging.getLogger("awgbot.routing")

# Сериализация перестроек: реконсиляция из планировщика и правка списка из
# хендлера могут прийти одновременно, а пересборка цепочки — read-modify-write.
mutation_lock = threading.RLock()


class RoutingError(Exception):
    """Ошибка взаимодействия с ipset/iptables/ip."""


class RoutingUnavailable(RoutingError):
    """Инструментов или обвяза нет — фича неработоспособна. Не ошибка бизнес-
    операции: UI её прячет, планировщик пропускает, VPN работает как обычно."""


# ─────────────────────────────────────────────────────────────────────────────
# Запуск команд: хост и контейнер
# ─────────────────────────────────────────────────────────────────────────────

def _host(args: list[str], *, check: bool = True,
          input_data: Optional[bytes] = None, timeout: int = 20):
    try:
        proc = subprocess.run(args, input=input_data, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)
    except FileNotFoundError as e:
        raise RoutingUnavailable(f"Команда не найдена на хосте: {args[0]} ({e})")
    except subprocess.TimeoutExpired:
        raise RoutingError(f"Таймаут команды: {' '.join(args[:4])}...")
    if check and proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        raise RoutingError(f"Ошибка {' '.join(args[:4])}: {err}")
    return proc


# Проверки «есть ли правило/маршрут/набор» — только чтение: их зовут тик и
# рендер экранов, и ждать 20 с ответа netlink там нечего — дольше 5 с это уже
# «не смог посмотреть» (третье состояние), а не отказ.
_PROBE_TIMEOUT = 5


def _host_ok(args: list[str]) -> bool:
    """Код возврата 0? Для проверок существования правил (iptables -C и т.п.)."""
    return _host(args, check=False, timeout=_PROBE_TIMEOUT).returncode == 0


def _cont(args: list[str], *, check: bool = True):
    """Команда внутри контейнера. Идёт через awg._exec — тот же путь, которым
    ставятся блокировки, чтобы способ доступа к контейнеру был ровно один."""
    try:
        proc = awg._exec(args, check=False)
    except awg.AwgError as e:
        raise RoutingError(f"Контейнер недоступен: {e}")
    if check and proc.returncode != 0:
        raise RoutingError(
            f"Ошибка в контейнере {' '.join(args[:4])}: "
            f"{proc.stderr.decode(errors='replace').strip()}")
    return proc


def _cont_ok(args: list[str]) -> bool:
    return _cont(args, check=False).returncode == 0


# ─────────────────────────────────────────────────────────────────────────────
# Имена наборов
# ─────────────────────────────────────────────────────────────────────────────

def user_set(client_id: int) -> str:
    """Набор личных доменов клиента."""
    return f"{config.ROUTING_SET_USER_PREFIX}{int(client_id)}"


def src_set(client_id: int) -> str:
    """Набор адресов устройств клиента (для пер-клиентского правила)."""
    return f"{config.ROUTING_SET_SRC_PREFIX}{int(client_id)}"


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

_MARK_HEX = f"0x{config.ROUTING_FWMARK:x}"


_PROBE_SET = "awgbot_rt_probe"

# Задержка последнего удавшегося коннекта — ПО МЕТКЕ: зонд фичи (0x1) и зонды
# слотов (1<<slot) ходят разными путями, и одна переменная на всех показывала
# бы задержку не того шлюза.
_last_probe_ms: dict[int, Optional[int]] = {}


def _active_if() -> str:
    """Интерфейс активного линка по умолчанию — из конфига: без слотов (одна
    машина, как до v2.24.0) это и есть единственный линк."""
    return config.ROUTING_GW_INTERFACE


# ── слоты шлюзов (концепт «резервный шлюз») ──────────────────────────────────
# У каждого слота своя таблица и свой бит метки — только ради зонда резерва:
# клиентский трафик ходит по таблице фичи, куда указывает активный линк.

def slot_table(slot_id: int) -> int:
    return config.ROUTING_TABLE + int(slot_id)


def slot_mark(slot_id: int) -> int:
    return 1 << int(slot_id)


def _slot_rule(slot_id: int) -> list[str]:
    m = f"0x{slot_mark(slot_id):x}"
    return ["fwmark", f"{m}/{m}", "lookup", str(slot_table(slot_id))]


def ensure_slot_policy(slot_id: int, iface: str) -> None:
    """Таблица и правило слота: путь зонда резерва через ЕГО линк. Идемпотентно."""
    _host(["ip", "route", "replace", "default", "dev", iface, "table", str(slot_table(slot_id))])
    rules = _host(["ip", "rule", "show"], check=False, timeout=_PROBE_TIMEOUT)
    text = rules.stdout.decode(errors="replace")
    if f"lookup {slot_table(slot_id)}" not in text:
        _host(["ip", "rule", "add", *_slot_rule(slot_id)])


def drop_slot_policy(slot_id: int, iface: str = "") -> None:
    """Снять таблицу и правило слота (слот убран). Отсутствие — не ошибка."""
    while _host_ok(["ip", "rule", "del", *_slot_rule(slot_id)]):
        pass
    _host(["ip", "route", "flush", "table", str(slot_table(slot_id))], check=False)


def link_present(iface: str) -> bool:
    return bool(iface) and _host_ok(["ip", "link", "show", iface])


def _check_host_tools() -> None:
    """Инструменты есть И ядро реально умеет то, что от него потребуется.

    Одной проверки `ipset -V` мало: на контейнерной виртуализации (OpenVZ/LXC)
    бинарь отработает, а `ipset create` упадёт — модулей ip_set в ядре гостя
    нет. Молча это вылезло бы уже после того, как кому-то включили режим.
    Поэтому наборы проверяем делом: создаём пробный и тут же сносим.
    """
    for tool in ("ipset", "iptables", "ip"):
        if not _host_ok([tool, "-V"]) and not _host_ok([tool, "--version"]):
            raise RoutingUnavailable(f"{tool} не установлен на хосте")

    _host(["ipset", "destroy", _PROBE_SET], check=False)
    probe = _host(["ipset", "create", _PROBE_SET, "hash:ip", "-exist"], check=False)
    if probe.returncode != 0:
        raise RoutingUnavailable(
            "ядро не поддерживает ipset ("
            + probe.stderr.decode(errors="replace").strip()
            + ") — на контейнерной виртуализации фича неработоспособна")
    _host(["ipset", "destroy", _PROBE_SET], check=False)

    # расширение xt_set: без него `-m set --match-set` не соберётся, а наборы
    # сами по себе ничего не маркируют
    if not _host_ok(["iptables", "-m", "set", "--help"]):
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
    units = _host(["systemctl", "list-unit-files", f"{svc}.service"], check=False)
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
    proc = _host(["ip", "route", "show", subnet], check=False, timeout=_PROBE_TIMEOUT)
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
        insp = _host(["docker", "inspect", "-f",
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
        nat_ok = _host_ok(["iptables", "-t", "nat", "-C", "POSTROUTING",
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
            if not _host_ok(["ip", "link", "show", config.ROUTING_GW_INTERFACE]):
                raise RoutingUnavailable(
                    f"интерфейс {config.ROUTING_GW_INTERFACE} не найден на хосте")
            # Только в docker-режиме: на хосте контейнерного плеча нет вовсе, и
            # проверка «доступен ли iptables в контейнере» гасила бы фичу из-за
            # отсутствия контейнера, которого там и не должно быть.
            if awg.in_container() and not _cont_ok(["iptables", "-t", "nat", "-L", "-n"]):
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


# ─────────────────────────────────────────────────────────────────────────────
# Плечо КОНТЕЙНЕРА: исключения из MASQUERADE
# ─────────────────────────────────────────────────────────────────────────────

def sync_nat_exempt(addresses) -> None:
    """Пересобрать цепочку исключений из MASQUERADE в контейнере.

    ACCEPT, а не RETURN: RETURN вернул бы пакет в POSTROUTING, где его подобрало
    бы следующее правило — то самое MASQUERADE, от которого мы уходим. ACCEPT в
    таблице nat завершает обход цепочки, и адрес остаётся настоящим.

    Хук вставляется ПЕРВЫМ правилом POSTROUTING: любое правило перед ним могло бы
    замаскарадить пакет раньше, чем мы до него доберёмся.

    В host-режиме — пусто. Не «нечего делать по случайности»: там MASQUERADE один
    и наш, клиент приходит на маркировку с настоящим адресом, и отменять нечего.
    Механизм исключений существовал ровно ради чужого MASQUERADE контейнера.
    """
    if not awg.in_container():
        return
    chain = config.ROUTING_NAT_CHAIN
    want = set()
    for addr in addresses:
        awg._validate_ip(addr)
        want.add(addr)
    # дифф перед записью: один `-S` вместо флаша и правила на устройство
    have = nat_exempt_addresses()
    if have != want:
        _cont(["iptables", "-t", "nat", "-N", chain], check=False)   # есть → код 1
        _cont(["iptables", "-t", "nat", "-F", chain])
        for addr in sorted(want):
            _cont(["iptables", "-t", "nat", "-A", chain,
                   "-s", f"{addr}/32", "-j", "ACCEPT"])
    if not _cont_ok(["iptables", "-t", "nat", "-C", "POSTROUTING", "-j", chain]):
        _cont(["iptables", "-t", "nat", "-I", "POSTROUTING", "1", "-j", chain])


def parse_nat_exempt(text: str, chain: str) -> Optional[set[str]]:
    """Адреса из `iptables -t nat -S <chain>`; None — цепочки нет или в ней
    правило чужой формы (тогда безопаснее пересобрать)."""
    out: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] in ("-N", "-P"):
            continue
        if parts[:2] != ["-A", chain] or "-s" not in parts or parts[-2:] != ["-j", "ACCEPT"]:
            return None
        out.add(parts[parts.index("-s") + 1].split("/")[0])
    return out


def nat_exempt_addresses() -> Optional[set[str]]:
    proc = _cont(["iptables", "-t", "nat", "-S", config.ROUTING_NAT_CHAIN], check=False)
    if proc.returncode != 0:
        return None
    return parse_nat_exempt(proc.stdout.decode(errors="replace"), config.ROUTING_NAT_CHAIN)


def ensure_set(name: str, kind: str, exists: bool = False) -> None:
    """Создать набор, если его нет. Содержимое НЕ трогает.

    Для доменных наборов это единственная допустимая операция со стороны бота:
    наполняет их dnsmasq по мере резолва, и любая перезапись стирала бы всё
    накопленное. Бот отвечает лишь за то, чтобы набор существовал к моменту,
    когда на него сошлётся правило или директива ipset=.
    exists=True — вызывающий уже видел набор в снимке `ipset save`: exec не нужен.
    """
    if exists:
        return
    _host(["ipset", "create", name, kind, "-exist"])


def parse_ipset_save(text: str) -> dict[str, set[str]]:
    """`ipset save` → {набор: множество членов}. Пустой набор — пустое
    множество: строка `create` есть, строк `add` нет."""
    sets: dict[str, set[str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "create":
            sets.setdefault(parts[1], set())
        elif len(parts) >= 3 and parts[0] == "add":
            sets.setdefault(parts[1], set()).add(parts[2])
    return sets


def snapshot_sets() -> Optional[dict[str, set[str]]]:
    """Состав всех наборов одним exec; None — не смогли прочитать (тогда
    вызывающий пересобирает всё безусловно, как раньше)."""
    proc = _host(["ipset", "save"], check=False, timeout=_PROBE_TIMEOUT)
    if proc.returncode != 0:
        return None
    return parse_ipset_save(proc.stdout.decode(errors="replace"))



def list_sets() -> list[str]:
    proc = _host(["ipset", "list", "-n"], check=False, timeout=_PROBE_TIMEOUT)
    return [l.strip() for l in proc.stdout.decode(errors="replace").splitlines() if l.strip()]


def replace_members(name: str, kind: str, members, current=None) -> None:
    """Атомарно заменить содержимое набора.

    Через временный набор и `ipset swap`, а не flush+add: flush оставляет окно, в
    котором набор пуст, и в это окно трафик уходит мимо маршрута. Для src-набора
    это означало бы моргание режима у пользователя на каждой реконсиляции.

    current — состав набора из снимка `ipset save` (None — снимка нет или
    набора в нём нет). Совпадает с желаемым — шесть exec не нужны.
    """
    members = list(members)
    if current is not None and set(members) == set(current):
        return
    tmp = f"{name}_tmp"
    ensure_set(name, kind, exists=current is not None)
    _host(["ipset", "destroy", tmp], check=False)
    ensure_set(tmp, kind)
    payload = "".join(f"add {tmp} {m}\n" for m in members)
    if payload:
        _host(["ipset", "restore", "-exist"], input_data=payload.encode())
    _host(["ipset", "swap", tmp, name])
    _host(["ipset", "destroy", tmp], check=False)


def destroy_set(name: str) -> None:
    _host(["ipset", "destroy", name], check=False)


# ─────────────────────────────────────────────────────────────────────────────
# Плечо ХОСТА: цепочка маркировки
# ─────────────────────────────────────────────────────────────────────────────

_MARK = f"{_MARK_HEX}/{_MARK_HEX}"


def _mangle(args: list[str], **kw):
    return _host(["iptables", "-t", "mangle", *args], **kw)


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
    desired = [(src_set(cid), user_set(cid)) for cid in sorted(client_ids)]
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
    return all(_host_ok(["iptables", "-t", "mangle", "-C", "PREROUTING", *hook])
               for hook in _hooks())


def _rule_present() -> bool:
    proc = _host(["ip", "rule", "show"], check=False, timeout=_PROBE_TIMEOUT)
    text = proc.stdout.decode(errors="replace")
    return _MARK_HEX in text and f"lookup {config.ROUTING_TABLE}" in text


def ensure_route(iface: str = "") -> None:
    """Маршрут по умолчанию в таблице фичи — в линк АКТИВНОГО шлюза."""
    _host(["ip", "route", "replace", "default", "dev", iface or _active_if(),
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
        pairs = [(n, _active_if()) for n in config.ROUTING_HOME_SUBNETS]
    for net, iface in pairs:
        try:
            ipaddress.ip_network(net, strict=False)
        except ValueError:
            log.warning("routing.home_subnets: %r не похоже на подсеть — пропущено", net)
            continue
        _host(["ip", "route", "replace", net, "dev", iface])


# ── Наблюдение за состоянием (только чтение; для диагностики) ────────────────

def rule_present() -> bool:
    """Стоит ли ip rule — постоянная обвязка, не рубильник."""
    return _rule_present()


def hook_present() -> bool:
    """Стоит ли РУБИЛЬНИК: прыгает ли PREROUTING в цепочку маркировки."""
    return _hook_present()


def table_route() -> Optional[str]:
    """Маршрут по умолчанию в таблице фичи, как его показывает ядро."""
    proc = _host(["ip", "route", "show", "default", "table",
                  str(config.ROUTING_TABLE)], check=False, timeout=_PROBE_TIMEOUT)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode(errors="replace").strip() or None


def mss_clamp_present(iface: str = "") -> bool:
    iface = iface or _active_if()
    if not iface:
        return False
    return _host_ok(["iptables", "-t", "mangle", "-C", "FORWARD",
                     "-o", iface, "-p", "tcp",
                     "--tcp-flags", "SYN,RST", "SYN",
                     "-j", "TCPMSS", "--clamp-mss-to-pmtu"])


def set_count(name: str) -> int:
    """Сколько записей в наборе — для диагностики.

    Пустой набор не авария (правило метит то, что В НАБОРЕ, значит из пустого
    следует «на шлюз не идёт ничего»), но означает, что режим не работает: либо
    списки не скачаны, либо dnsmasq не может писать в ipset — а это самый
    молчаливый из отказов, резолв при нём исправен."""
    proc = _host(["ipset", "list", name, "-t"], check=False, timeout=_PROBE_TIMEOUT)
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
    iface = iface or _active_if()
    if not iface:
        return
    rule = ["-o", iface, "-p", "tcp",
            "--tcp-flags", "SYN,RST", "SYN",
            "-j", "TCPMSS", "--clamp-mss-to-pmtu"]
    if not _host_ok(["iptables", "-t", "mangle", "-C", "FORWARD", *rule]):
        _host(["iptables", "-t", "mangle", "-A", "FORWARD", *rule])
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
    active_iface = active_iface or _active_if()
    if not active_iface:
        return
    ensure_route(active_iface)
    if slots:
        pairs = [(net, iface) for _sid, iface, nets in slots for net in nets]
        ensure_home_routes(pairs)
        for sid, iface, _nets in slots:
            ensure_slot_policy(sid, iface)
            ensure_mss_clamp(iface)
    else:
        ensure_home_routes()
        ensure_mss_clamp(active_iface)
    if not _rule_present():
        _host(["ip", "rule", "add", *_RULE])


def set_marking_enabled(on: bool) -> None:
    """Главный рубильник: прыгает ли PREROUTING в цепочку маркировки.

    Выключение — механизм ГРАЦИОЗНОЙ ДЕГРАДАЦИИ при непроходимом шлюзе: метить
    перестаём, трафик уходит обычным путём с зарубежным адресом. Пользователь
    видит «банк ругается на IP», а не «интернета нет». Идемпотентно.
    """
    if not config.ROUTING_GW_INTERFACE:
        return
    hooks = _hooks()
    if on:
        missing = [h for h in hooks
                   if not _host_ok(["iptables", "-t", "mangle", "-C", "PREROUTING", *h])]
        if missing:
            ensure_policy()
            for hook in missing:
                _mangle(["-I", "PREROUTING", *hook])
            log.info("routing: маркировка включена (%d подсет.)", len(hooks))
        return
    removed = False
    for hook in hooks:
        # Снимаем ВСЕ копии: дубли хука мог оставить прошлый запуск, и одного
        # -D тогда мало — режим остался бы включённым при снятом рубильнике.
        while _host_ok(["iptables", "-t", "mangle", "-C", "PREROUTING", *hook]):
            _mangle(["-D", "PREROUTING", *hook], check=False)
            removed = True
    if removed:
        log.info("routing: маркировка снята (деградация)")


# ─────────────────────────────────────────────────────────────────────────────
# Живость линка до шлюза
# ─────────────────────────────────────────────────────────────────────────────

def link_peer_address(iface: str = "") -> Optional[str]:
    """Адрес шлюза в линке. Линк — /30, адресов ровно два, наш известен из
    `ip addr`, значит второй вычисляется однозначно. Спрашивать конфиг не нужно:
    ядро — более достоверный источник, чем файл, который могли и не применить.
    """
    proc = _host(["ip", "-4", "-o", "addr", "show", "dev",
                  iface or _active_if()], check=False, timeout=_PROBE_TIMEOUT)
    if proc.returncode != 0:
        return None
    for tok in proc.stdout.decode(errors="replace").split():
        if "/" not in tok or tok.count(".") != 3:
            continue
        try:
            net = ipaddress.ip_interface(tok)
        except ValueError:
            continue
        if net.network.prefixlen != 30:
            return None
        peers = [h for h in net.network.hosts() if h != net.ip]
        return str(peers[0]) if peers else None
    return None


# Что показал зонд. Различать «линк лёг» и «шлюз есть, но интернета за ним нет»
# нужно не ради красоты: во втором случае туннель отвечает на хендшейки, и любая
# проверка по возрасту хендшейка считает шлюз живым — а трафик за ним умирает.
PROBE_OK = "ok"          # через шлюз ходит трафик наружу
PROBE_NO_PATH = "path"   # шлюз отвечает, но наружу через него не пройти
PROBE_DOWN = "down"      # шлюз не отвечает вовсе


def _tcp_probe(host: str, port: int, timeout: float, mark: Optional[int] = None) -> bool:
    """TCP-коннект С МЕТКОЙ фичи — то есть ровно тем путём, которым ходит
    клиентский трафик: ip rule по метке → таблица фичи → линк → шлюз → его NAT.

    Именно TCP, а не ICMP. Предыдущая версия пинговала, и зонд падал на том,
    чего от шлюза никто не требовал: наружу пинг уходил по main-таблице (где
    маршрута в шлюз нет — он лежит в таблице фичи), а до самого шлюза не
    доходил, потому что INPUT на нём ICMP не разрешает. Проверялся путь,
    который никогда не был настроен, а настоящий — не проверялся вовсе.
    """
    mark = config.ROUTING_FWMARK if mark is None else int(mark)
    so_mark = getattr(socket, "SO_MARK", 36)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    started = time.monotonic()
    try:
        sock.setsockopt(socket.SOL_SOCKET, so_mark, mark)
        sock.settimeout(timeout)
        sock.connect((host, port))
        _last_probe_ms[mark] = int((time.monotonic() - started) * 1000)
        return True
    except OSError:
        _last_probe_ms[mark] = None
        return False
    finally:
        sock.close()


def last_probe_latency_ms(mark: Optional[int] = None) -> Optional[int]:
    """Сколько занял последний удавшийся коннект (по метке: без неё — зонд
    фичи). Для диагностики: «медленно» и «мёртво» снаружи неотличимы, а чинятся
    совершенно по-разному. Растущая задержка на пути через шлюз — обычно
    признак того, что туннель на нём считается в userspace, а не ядром."""
    return _last_probe_ms.get(config.ROUTING_FWMARK if mark is None else int(mark))


def ping_peer(iface: str = "", count: int = 3, timeout: float = 2.0) -> Optional[int]:
    """«Пинг до шлюза»: ICMP с ВПС на адрес шлюза в линке, медиана RTT в мс.
    Файервол шлюза пускает ICMP с ВПС по линку. None — не отвечает или
    линка нет. Локальная команда, наружу с домашнего адреса ничего не уходит."""
    import re as _re
    peer = link_peer_address(iface)
    if not peer:
        return None
    proc = _host(["ping", "-c", str(count), "-i", "0.3", "-W", str(int(timeout)), peer],
                 check=False, timeout=int(count * (timeout + 0.5)) + 3)
    if proc.returncode != 0 and b"time=" not in proc.stdout:
        return None
    vals = sorted(float(m) for m in _re.findall(r"time=([\d.]+) ms",
                                                 proc.stdout.decode(errors="replace")))
    return int(round(vals[len(vals) // 2])) if vals else None


def link_peer_endpoint(iface: str = "") -> Optional[str]:
    """Внешний адрес шлюза — из эндпоинта пира линка: ВПС видит его с каждым
    хендшейком, спрашивать сторонние сервисы «мой IP» с домашнего адреса
    незачем. None — пира нет или хендшейка ещё не было."""
    proc = _host(["awg", "show", iface or _active_if(), "dump"], check=False)
    if proc.returncode != 0:
        return None
    for p_ in awg.parse_dump(proc.stdout.decode(errors="replace")):
        ep = p_.get("endpoint")
        if ep:
            host = ep.rsplit(":", 1)[0].strip("[]")
            try:
                return str(ipaddress.ip_address(host))
            except ValueError:
                return host
    return None


def probe_source(iface: str = "") -> Optional[str]:
    """С какого адреса уходит зонд — то есть и весь маркированный трафик.

    Полезно ровно в разборе отказа: шлюз маскарадит клиентскую подсеть, а зонд
    идёт с адреса линка, и если для него маскарада нет, пакеты уходят наружу с
    немаршрутизируемым адресом. Снаружи это неотличимо от «у шлюза нет
    интернета», поэтому адрес называем прямо.
    """
    proc = _host(["ip", "-4", "-o", "addr", "show", "dev",
                  iface or _active_if()], check=False, timeout=_PROBE_TIMEOUT)
    if proc.returncode != 0:
        return None
    for tok in proc.stdout.decode(errors="replace").split():
        if "/" in tok and tok.count(".") == 3:
            return tok.split("/")[0]
    return None


def probe_gateway(target, port: int = 53, attempts: int = 2,
                  timeout: float = 4.0, *, iface: str = "",
                  mark: Optional[int] = None) -> str:
    """Проходит ли трафик НАРУЖУ через шлюз. Возвращает PROBE_*.

    Меряем то, что важно, а не то, что легко померить: возраст хендшейка
    отвечает на вопрос «поднят ли туннель», а нас интересует «дойдёт ли пакет
    до интернета». Эти условия расходятся ровно в худшем случае — у шлюза упал
    аплинк или слетел форвардинг, а хендшейки идут как ни в чём не бывало.

    Потери гасим повторами ВНУТРИ замера: одиночный потерянный пакет — шум, а
    не отвал, и растягивать из-за него решение на минуты незачем.
    """
    iface = iface or _active_if()
    if not iface:
        return PROBE_DOWN
    # ensure_policy() здесь НЕ зовём, хотя без правила и маршрута зонд проверит
    # не тот путь. Измеритель, который чинит измеряемое, — не измеритель:
    # диагностика вызывает эту же функцию и рапортовала бы «ip rule есть» ровно
    # потому, что сама его только что создала, то есть отсутствие обвязки не
    # обнаруживалось бы никогда. Доводит её тот, кто управляет состоянием, —
    # routing_liveness_tick.
    # НЕСКОЛЬКО целей, а не одна. Один внешний хост — сам по себе точка отказа:
    # он может лечь, подтормаживать или резать частые коннекты, и тогда мы
    # объявим отказом шлюза чужую проблему. Успех любой цели означает, что путь
    # наружу есть, — а это ровно то, что мы и хотим знать.
    targets = [target] if isinstance(target, str) else list(target)
    for _ in range(max(1, attempts)):
        if _any_reachable(targets, port, timeout, mark):
            return PROBE_OK
    # Наружу не прошли. Различаем, где чинить, по состоянию туннеля — здесь это
    # уместно: решение о живости уже принято выше, хендшейк лишь уточняет адрес
    # ремонта. Свежий хендшейк ⇒ туннель жив, значит дело за шлюзом.
    age = link_handshake_age(iface)
    return PROBE_NO_PATH if (age is not None and age <= 180) else PROBE_DOWN


def _any_reachable(targets: list, port: int, timeout: float,
                   mark: Optional[int] = None) -> bool:
    """Одна попытка по всем целям РАЗОМ: цели независимы, а последовательный
    обход в худшем случае держал рабочий поток 2 × N × 4 с. Первый успех —
    ответ, остальные коннекты дотикают свой таймаут в фоне."""
    if len(targets) == 1:
        return _tcp_probe(targets[0], port, timeout, mark)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    # без `with`: выход из контекста ждёт ВСЕ потоки, а нам нужен первый успех —
    # остальные коннекты дотикают свой таймаут в фоне
    pool = ThreadPoolExecutor(max_workers=len(targets))
    try:
        futs = [pool.submit(_tcp_probe, h, port, timeout, mark) for h in targets]
        for f in as_completed(futs):
            if f.result():
                return True
        return False
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def link_handshake_age(iface: str = "") -> Optional[int]:
    """Возраст последнего хендшейка с шлюзом, сек. None — пира нет или интерфейс
    не отвечает. Линк живёт на ХОСТЕ, поэтому и awg спрашиваем на хосте.
    """
    proc = _host(["awg", "show", iface or _active_if(), "dump"], check=False)
    if proc.returncode != 0:
        return None
    peers = awg.parse_dump(proc.stdout.decode(errors="replace"))
    stamps = [p["last_handshake"] for p in peers if p.get("last_handshake")]
    if not stamps:
        return None
    return max(0, int(time.time()) - max(stamps))


# ─────────────────────────────────────────────────────────────────────────────
# Внешние списки и конфиг dnsmasq
# ─────────────────────────────────────────────────────────────────────────────

def fetch(url: str, timeout: int = 15) -> tuple[Optional[str], str, int]:
    """Скачать текст. Возвращает (тело, ошибка, код ответа).

    Не исключение: недоступность апстрима не должна ронять ни старт бота, ни
    цикл мониторинга. Прежний набор при этом остаётся в силе — застывший список
    всё ещё покрывает большинство сервисов, а пустой не покрывает ни одного.

    Но и не молчаливый None: «не достучались», «файла нет по адресу» и
    «ответили пустым» лечатся в разных местах, а дальше все трое выглядят нулём
    записей. Различить их можно только здесь, поэтому наружу идут и текст
    ошибки, и код: 404 означает переехавший файл, 429 — наш собственный лимит,
    и совет по ним прямо противоположный.

    Код — 0, когда HTTP-ответа не было вовсе (DNS, TLS, таймаут).
    """
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "awg-bot"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace"), "", 200
    except urllib.error.HTTPError as e:
        log.warning("routing: не скачался %s (%s)", url, e)
        return None, (str(e) or e.__class__.__name__)[:200], int(e.code)
    except Exception as e:                                # noqa: BLE001
        log.warning("routing: не скачался %s (%s)", url, e)
        return None, (str(e) or e.__class__.__name__)[:200], 0


def resolve_a(domain: str, timeout: float = 3.0) -> list[str]:
    """IPv4-адреса домена. Пустой список — не разрезолвилось; это не ошибка.

    Нужно, чтобы набор не стоял пустым до первого DNS-запроса клиента. Домен
    добавляют ровно тогда, когда сайт только что не открылся, — значит адрес
    уже лежит в кэше браузера, клиент переспросит DNS не скоро, а до тех пор
    пойдёт мимо набора. Резолвим сами и досеиваем.

    Ответ системного резолвера может отличаться от того, что dnsmasq отдаст
    клиенту (CDN крутит адреса). Это не мешает: адреса ДОПИСЫВАЮТСЯ, и запрос
    клиента добавит свои.

    Цена лишнего адреса в наборе теперь ДРУГАЯ, чем была. В обратной модели он
    означал «поедет за границу» и был безобиден. Сейчас он означает «поедет
    домой»: досев по домену на общем хостинге может утащить в домашний канал
    чужой адрес. Эффект заперт в наборе одного профиля и снимается удалением
    домена из его списка, но безобидным его называть больше нельзя.
    """
    try:
        socket.setdefaulttimeout(timeout)
        info = socket.getaddrinfo(domain, None, family=socket.AF_INET,
                                  type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    finally:
        socket.setdefaulttimeout(None)
    return sorted({i[4][0] for i in info})


def add_networks(name: str, members) -> int:
    """Дописать подсети в набор. Возвращает число принятых.

    ДОПИСАТЬ, а не заменить: в том же наборе живут адреса, которые накопил
    dnsmasq по мере резолва доменов, и перезапись стёрла бы их — маркировать
    стало бы нечем до следующего запроса к каждому домену.

    Через `ipset restore` одной пачкой, а не по команде на запись: списки
    измеряются сотнями строк на профиль, и раздельные вызовы растянули бы
    обновление. `-exist` делает операцию идемпотентной.
    """
    ensure_set(name, "hash:net")
    payload = "".join(f"add {name} {m} -exist\n" for m in members)
    if not payload:
        return 0
    _host(["ipset", "restore", "-exist"], input_data=payload.encode())
    return payload.count("\n")


def write_dnsmasq_conf(text: str, path: str = None) -> bool:
    """Записать конфиг списков и применить. True — файл изменился (был рестарт).

    Дифф-скип обязателен: реконсиляция ходит по расписанию, а рестарт dnsmasq
    роняет кэш ВСЕМ клиентам. Перезапускать при неизменном содержимом — значит
    регулярно портить резолвинг на ровном месте.

    Именно restart, а не reload: SIGHUP перечитывает hosts и чистит кэш, но НЕ
    конфиг-файлы — новые директивы ipset= через reload не подхватываются.
    """
    path = path or config.ROUTING_DNSMASQ_CONF
    try:
        with open(path, "r", encoding="utf-8") as f:
            if f.read() == text:
                return False
    except FileNotFoundError:
        pass
    except OSError as e:
        raise RoutingError(f"Не прочитать {path}: {e}")

    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)                     # атомарно: без полуфайла
    except OSError as e:
        raise RoutingError(f"Не записать {path}: {e}")

    proc = subprocess.run(
        ["systemctl", "restart", config.ROUTING_DNSMASQ_SERVICE],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if proc.returncode != 0:
        raise RoutingError(
            "Не перезапустить dnsmasq: " + proc.stderr.decode(errors="replace").strip())
    return True


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
    "rebuild_chain", "set_marking_enabled", "link_handshake_age",
    "probe_gateway", "link_peer_address", "resolve_a",
    "PROBE_OK", "PROBE_NO_PATH", "PROBE_DOWN",
    "ensure_mss_clamp", "mss_clamp_present",
    "rule_present", "table_route", "set_count", "hook_present", "ensure_policy",
    "probe_source", "last_probe_latency_ms",
    # внешние списки и dnsmasq
    "fetch", "write_dnsmasq_conf",
]
