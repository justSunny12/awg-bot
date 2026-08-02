"""
routing.py — единственный слой команд условной маршрутизации.

Отношение к domain/routing.py то же, что у awg.py к configgen.py: там чистые
преобразования, здесь всё, что дёргает систему.

ДВА ПЛЕЧА. Контейнер Amnezia работает в режиме bridge, то есть в собственном
сетевом namespace, а ipset — сущность per-netns: набор, созданный внутри
контейнера, для dnsmasq на хосте просто не существует и наполняться не будет.
При этом различать клиентов можно только ДО MASQUERADE контейнера — за ним все
пиры выглядят одним bridge-адресом (ровно поэтому там же живут блокировки, см.
awg.block_ip). Отсюда разделение:

  • в КОНТЕЙНЕРЕ — только исключения из MASQUERADE: по одному правилу на
    включённое устройство. Ничего, кроме iptables, там не требуется;
  • на ХОСТЕ — наборы ipset, маркировка, ip rule, dnsmasq и линк до шлюза.

Работает это потому, что немаскараженный трафик приходит на хост с настоящим
10.8.1.x, а весь остальной — с bridge-адреса контейнера. Различение достаётся
даром, без передачи метки между namespace (она бы и не пережила переход).

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
import subprocess
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


def _host_ok(args: list[str]) -> bool:
    """Код возврата 0? Для проверок существования правил (iptables -C и т.п.)."""
    return _host(args, check=False).returncode == 0


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

_MARK_HEX = f"0x{config.ROUTING_FWMARK:x}"


_PROBE_SET = "awgbot_rt_probe"


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

    # Базовый набор описывает то, чему нужна ЗАГРАНИЦА, а правило работает от
    # обратного. Пустой набор означает «на шлюз уходит всё», включая заблокированное,
    # которое с российского адреса не откроется. Это не мелкая недостача, а
    # состояние, при котором включать функцию нельзя.
    probe = _host(["ipset", "list", config.ROUTING_SET_BASE], check=False)
    if probe.returncode == 0:
        entries = sum(1 for l in probe.stdout.decode(errors="replace").splitlines()
                      if l[:1].isdigit())
        if entries == 0:
            raise RoutingUnavailable(
                f"набор {config.ROUTING_SET_BASE} пуст — при инверсии логики это "
                f"отправило бы на шлюз ВЕСЬ трафик. Запустите "
                f"install/routing-lists-update.sh")


def _check_static_plumbing() -> None:
    """Проверить обвяз, который ставится при развёртывании, а не ботом.

    Без него фича «работает» ровно до первого пакета: правила соберутся, наборы
    наполнятся, а трафик включённого устройства либо не выйдет с хоста, либо не
    вернётся в контейнер. Снаружи это выглядит как «интернет пропал у того, кому
    включили режим» — то есть худший из возможных отказов, поэтому проверяем
    явно и до того, как кого-то включат.
    """
    subnet = config.ROUTING_CLIENT_SUBNET
    # именно `route show <подсеть>`, а не `route get <адрес>`: get вернёт код 0
    # практически всегда, потому что подойдёт маршрут по умолчанию, — проверка
    # была бы ложноположительной. Нужен ЯВНЫЙ маршрут для этой подсети.
    proc = _host(["ip", "route", "show", subnet], check=False)
    route = proc.stdout.decode(errors="replace") if proc.returncode == 0 else ""
    if not route.strip():
        raise RoutingUnavailable(
            f"на хосте нет маршрута до {subnet} — обратный трафик не вернётся "
            f"в контейнер")
    # Маршрут может существовать, но вести на СТАРЫЙ адрес: контейнер пересоздали,
    # docker выдал ему другой IP, а маршрут остался прежним. Снаружи это выглядит
    # как «у включённых пропал интернет» без единой ошибки в логах.
    insp = _host(["docker", "inspect", "-f",
                  "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                  config.CONTAINER], check=False)
    cont_ip = insp.stdout.decode(errors="replace").split()
    if insp.returncode == 0 and cont_ip and cont_ip[0] not in route:
        raise RoutingUnavailable(
            f"маршрут до {subnet} ведёт не на текущий адрес контейнера "
            f"({cont_ip[0]}) — контейнер пересоздавали? перезапустите "
            f"awg-bot-routing.service")
    if not _host_ok(["iptables", "-t", "nat", "-C", "POSTROUTING",
                     "-s", subnet, "-j", "MASQUERADE"]):
        raise RoutingUnavailable(
            f"на хосте нет MASQUERADE для {subnet} — трафик включённых устройств "
            f"не выйдет наружу")
    # Именно СЕРВИС, а не бинарь: пакет dnsmasq-base кладёт /usr/sbin/dnsmasq без
    # systemd-юнита, и `systemctl restart` при каждой правке списка падал бы.
    svc = config.ROUTING_DNSMASQ_SERVICE
    units = _host(["systemctl", "list-unit-files", f"{svc}.service"], check=False)
    if f"{svc}.service" not in units.stdout.decode(errors="replace"):
        raise RoutingUnavailable(
            f"нет юнита {svc}.service (установлен только dnsmasq-base?) — "
            f"применить списки доменов будет нечем")


def self_check(force: bool = False) -> tuple[bool, str]:
    """(работоспособна ли фича, причина). Кэшируется: зовётся из preflight и из
    каждого тика планировщика, а меняется только при переустановке окружения."""
    global _selfcheck_cache
    if _selfcheck_cache is not None and not force:
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
            if not _cont_ok(["iptables", "-t", "nat", "-L", "-n"]):
                raise RoutingUnavailable("iptables недоступен в контейнере")
            _check_static_plumbing()
            result = (True, "ок")
        except RoutingUnavailable as e:
            result = (False, str(e))
        except RoutingError as e:
            result = (False, f"проверка не удалась: {e}")

    _selfcheck_cache = result
    if not result[0]:
        log.warning("routing: фича неактивна — %s", result[1])
    return result


def available() -> bool:
    return self_check()[0]


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
    """
    chain = config.ROUTING_NAT_CHAIN
    _cont(["iptables", "-t", "nat", "-N", chain], check=False)   # есть → код 1
    _cont(["iptables", "-t", "nat", "-F", chain])
    for addr in addresses:
        awg._validate_ip(addr)
        _cont(["iptables", "-t", "nat", "-A", chain,
               "-s", f"{addr}/32", "-j", "ACCEPT"])
    if not _cont_ok(["iptables", "-t", "nat", "-C", "POSTROUTING", "-j", chain]):
        _cont(["iptables", "-t", "nat", "-I", "POSTROUTING", "1", "-j", chain])


def drop_nat_exempt() -> None:
    """Снять исключения целиком (полное выключение фичи)."""
    chain = config.ROUTING_NAT_CHAIN
    _cont(["iptables", "-t", "nat", "-D", "POSTROUTING", "-j", chain], check=False)
    _cont(["iptables", "-t", "nat", "-F", chain], check=False)
    _cont(["iptables", "-t", "nat", "-X", chain], check=False)


# ─────────────────────────────────────────────────────────────────────────────
# Плечо ХОСТА: наборы
# ─────────────────────────────────────────────────────────────────────────────

def ensure_set(name: str, kind: str) -> None:
    """Создать набор, если его нет. Содержимое НЕ трогает.

    Для доменных наборов это единственная допустимая операция со стороны бота:
    наполняет их dnsmasq по мере резолва, и любая перезапись стирала бы всё
    накопленное. Бот отвечает лишь за то, чтобы набор существовал к моменту,
    когда на него сошлётся правило или директива ipset=.
    """
    _host(["ipset", "create", name, kind, "-exist"])


_ensure_set = ensure_set          # прежнее приватное имя (внутренние вызовы)


def list_sets() -> list[str]:
    proc = _host(["ipset", "list", "-n"], check=False)
    return [l.strip() for l in proc.stdout.decode(errors="replace").splitlines() if l.strip()]


def replace_members(name: str, kind: str, members) -> None:
    """Атомарно заменить содержимое набора.

    Через временный набор и `ipset swap`, а не flush+add: flush оставляет окно, в
    котором набор пуст, и в это окно трафик уходит мимо маршрута. Для src-набора
    это означало бы моргание режима у пользователя на каждой реконсиляции.
    """
    tmp = f"{name}_tmp"
    _ensure_set(name, kind)
    _host(["ipset", "destroy", tmp], check=False)
    _ensure_set(tmp, kind)
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

    Правило на клиента — ОТ ОБРАТНОГО: «источник его, а назначение НЕ в списке
    тех, кому нужна заграница» → на шлюз. Так российский адрес достаётся всему,
    что не заблокировано, включая сервисы на .com, и вести перечень российских
    сайтов не приходится вовсе.

    Оба отрицания в одном правиле дают пересечение: мимо шлюза уходит и то, что
    в базовом списке, и то, что клиент добавил себе лично.
    """
    chain = config.ROUTING_CHAIN
    _mangle(["-N", chain], check=False)          # уже есть → код 1, это норма
    _mangle(["-F", chain])
    for cid in sorted(client_ids):
        _mangle(["-A", chain,
                 "-m", "set", "--match-set", src_set(cid), "src",
                 "-m", "set", "!", "--match-set", config.ROUTING_SET_BASE, "dst",
                 "-m", "set", "!", "--match-set", user_set(cid), "dst",
                 "-j", "MARK", "--set-xmark", _MARK])
    # хук в PREROUTING — идемпотентно. Сужен клиентской подсетью: до цепочки
    # доходит только немаскараженный трафик включённых устройств, остальному в
    # ней делать нечего.
    hook = ["-t", "mangle", "-C", "PREROUTING",
            "-s", config.ROUTING_CLIENT_SUBNET, "-j", chain]
    if not _host_ok(["iptables", *hook]):
        _mangle(["-I", "PREROUTING", "-s", config.ROUTING_CLIENT_SUBNET, "-j", chain])


def drop_chain() -> None:
    """Снять хук и цепочку целиком (полное выключение фичи)."""
    chain = config.ROUTING_CHAIN
    _mangle(["-D", "PREROUTING", "-s", config.ROUTING_CLIENT_SUBNET, "-j", chain],
            check=False)
    _mangle(["-F", chain], check=False)
    _mangle(["-X", chain], check=False)


# ─────────────────────────────────────────────────────────────────────────────
# Плечо ХОСТА: политика маршрутизации (и рубильник деградации)
# ─────────────────────────────────────────────────────────────────────────────

_RULE = ["fwmark", str(config.ROUTING_FWMARK), "table", str(config.ROUTING_TABLE)]


def _rule_present() -> bool:
    proc = _host(["ip", "rule", "show"], check=False)
    text = proc.stdout.decode(errors="replace")
    return _MARK_HEX in text and f"lookup {config.ROUTING_TABLE}" in text


def ensure_route() -> None:
    """Маршрут по умолчанию в таблице фичи — в линк-туннель до шлюза."""
    _host(["ip", "route", "replace", "default", "dev", config.ROUTING_GW_INTERFACE,
           "table", str(config.ROUTING_TABLE)])


def set_marking_enabled(on: bool) -> None:
    """Главный рубильник: есть ли ip rule, отправляющий помеченное в таблицу.

    Выключение — механизм ГРАЦИОЗНОЙ ДЕГРАДАЦИИ при недоступном шлюзе: маркировка
    остаётся, но никуда не ведёт, и трафик уходит обычным путём с зарубежным
    адресом. Пользователь видит «банк ругается на IP», а не «интернета нет».
    Идемпотентно.
    """
    present = _rule_present()
    if on and not present:
        ensure_route()
        _host(["ip", "rule", "add", *_RULE])
        log.info("routing: политика включена")
    elif not on and present:
        _host(["ip", "rule", "del", *_RULE], check=False)
        log.info("routing: политика выключена (деградация)")


# ─────────────────────────────────────────────────────────────────────────────
# Живость линка до шлюза
# ─────────────────────────────────────────────────────────────────────────────

def link_handshake_age() -> Optional[int]:
    """Возраст последнего хендшейка с шлюзом, сек. None — пира нет или интерфейс
    не отвечает. Линк живёт на ХОСТЕ, поэтому и awg спрашиваем на хосте.
    """
    proc = _host(["awg", "show", config.ROUTING_GW_INTERFACE, "dump"], check=False)
    if proc.returncode != 0:
        return None
    peers = awg.parse_dump(proc.stdout.decode(errors="replace"))
    stamps = [p["last_handshake"] for p in peers if p.get("last_handshake")]
    if not stamps:
        return None
    import time as _t
    return max(0, int(_t.time()) - max(stamps))


# ─────────────────────────────────────────────────────────────────────────────
# Конфиг dnsmasq (на хосте)
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Наполнение базового набора
# ─────────────────────────────────────────────────────────────────────────────

def fetch(url: str, timeout: int = 60) -> Optional[str]:
    """Скачать текст. None при любой неудаче — вызывающий сам решит, что делать.

    Молчаливый None, а не исключение: недоступность апстрима не должна ронять
    ни старт бота, ни цикл мониторинга. Прежний набор при этом остаётся в силе —
    устаревшие списки лучше пустых, потому что пустой отправил бы на шлюз всё.
    """
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "awg-bot"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:                                # noqa: BLE001
        log.warning("routing: не скачался %s (%s)", url, e)
        return None


def add_networks(members) -> int:
    """Залить подсети в базовый набор. Возвращает число принятых.

    Через `ipset restore` одной пачкой, а не по одной команде на запись: списки
    измеряются тысячами строк, и раздельные вызовы растянули бы обновление на
    минуты. `-exist` делает операцию идемпотентной — набор пополняется, а не
    пересоздаётся, поэтому то, что уже накопил dnsmasq, остаётся на месте.
    """
    ensure_set(config.ROUTING_SET_BASE, "hash:net")
    payload = "".join(f"add {config.ROUTING_SET_BASE} {m} -exist\n" for m in members)
    if not payload:
        return 0
    _host(["ipset", "restore", "-exist"], input_data=payload.encode())
    return payload.count("\n")


def base_set_size() -> int:
    """Сколько записей в базовом наборе (0 — в том числе если его нет)."""
    proc = _host(["ipset", "list", config.ROUTING_SET_BASE], check=False)
    if proc.returncode != 0:
        return 0
    return sum(1 for l in proc.stdout.decode(errors="replace").splitlines()
               if l[:1].isdigit())


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
    "self_check", "available", "user_set", "src_set",
    "sync_nat_exempt", "drop_nat_exempt",
    "ensure_set", "replace_members", "destroy_set", "list_sets",
    "rebuild_chain", "drop_chain", "ensure_route", "set_marking_enabled",
    "link_handshake_age", "write_dnsmasq_conf",
]
