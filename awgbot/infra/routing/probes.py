"""probes.py — живость линка: адрес и состояние пира, зонды, пинг, возраст рукопожатия."""

from __future__ import annotations

import ipaddress
import socket
import time
from typing import Optional

from awgbot.core import config
from awgbot.infra import awg

from . import base

_PROBE_SET = "awgbot_rt_probe"

# Задержка последнего удавшегося коннекта — ПО МЕТКЕ: зонд фичи (0x1) и зонды
# слотов (1<<slot) ходят разными путями, и одна переменная на всех показывала
# бы задержку не того шлюза.
_last_probe_ms: dict[int, Optional[int]] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Живость линка до шлюза
# ─────────────────────────────────────────────────────────────────────────────

def link_peer_address(iface: str = "") -> Optional[str]:
    """Адрес шлюза в линке. Линк — /30, адресов ровно два, наш известен из
    `ip addr`, значит второй вычисляется однозначно. Спрашивать конфиг не нужно:
    ядро — более достоверный источник, чем файл, который могли и не применить.
    """
    proc = base._host(["ip", "-4", "-o", "addr", "show", "dev",
                  iface or base._active_if()], check=False, timeout=base._PROBE_TIMEOUT)
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
    proc = base._host(["ping", "-c", str(count), "-i", "0.3", "-W", str(int(timeout)), peer],
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
    proc = base._host(["awg", "show", iface or base._active_if(), "dump"], check=False,
                 timeout=base._PROBE_TIMEOUT)
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
    proc = base._host(["ip", "-4", "-o", "addr", "show", "dev",
                  iface or base._active_if()], check=False, timeout=base._PROBE_TIMEOUT)
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
    iface = iface or base._active_if()
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


def link_peer_state(iface: str = "") -> Optional[dict]:
    """Хендшейк и счётчики пира линка одним снимком: {"age", "rx", "tx"}.

    Один `awg show dump` вместо двух: возраст хендшейка и байты приходят из
    одной строки, а спрашивают их в одном такте и по одному поводу — живо ли
    направление. None — пира нет или интерфейс не отвечает.

    Счётчики тут не для отчётности, а для УЛИК: выросший rx означает, что
    шлюз прислал через линк настоящий обратный трафик клиентов, и путь
    ВПС → линк → шлюз → интернет → обратно уже доказан — без единого пакета
    наружу. Линк живёт на ХОСТЕ, поэтому и awg спрашиваем на хосте.

    Таймаут пробы, а не дефолтные 20 с: это чтение идёт КАЖДЫМ тактом живости
    на каждый слот, и подвисший netlink (перезапуск awg-quick под нагрузкой)
    держал бы поток пула до двадцати секунд, съедая следующий такт целиком —
    задание с max_instances=1.
    """
    proc = base._host(["awg", "show", iface or base._active_if(), "dump"], check=False,
                 timeout=base._PROBE_TIMEOUT)
    if proc.returncode != 0:
        return None
    peers = awg.parse_dump(proc.stdout.decode(errors="replace"))
    if not peers:
        return None
    stamps = [p["last_handshake"] for p in peers if p.get("last_handshake")]
    age = max(0, int(time.time()) - max(stamps)) if stamps else None
    return {"age": age,
            "rx": sum(int(p.get("rx") or 0) for p in peers),
            "tx": sum(int(p.get("tx") or 0) for p in peers)}


def link_handshake_age(iface: str = "") -> Optional[int]:
    """Возраст последнего хендшейка с шлюзом, сек. None — пира нет, интерфейс
    не отвечает или хендшейка не было вовсе."""
    st = link_peer_state(iface)
    return st["age"] if st else None
