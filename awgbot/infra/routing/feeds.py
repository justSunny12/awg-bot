"""feeds.py — внешние списки и конфиг dnsmasq: загрузка, резолв, добавление сетей, запись конфига."""

from __future__ import annotations

import os
import socket
import subprocess
from typing import Optional

from awgbot.core import config

from . import base, sets
from .base import log, RoutingError

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
    from concurrent import futures
    from awgbot.util.nets import getaddrinfo_timed
    try:
        info = getaddrinfo_timed(domain, timeout=timeout, family=socket.AF_INET)
    except (OSError, UnicodeError, futures.TimeoutError):
        return []
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
    sets.ensure_set(name, "hash:net")
    payload = "".join(f"add {name} {m} -exist\n" for m in members)
    if not payload:
        return 0
    base._host(["ipset", "restore", "-exist"], input_data=payload.encode())
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
    old: str | None = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            old = f.read()
            if old == text:
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

    # Синтаксис — до рестарта, с conf-dir (голый --test файлы каталога не читает):
    # битая строка иначе роняла бы DNS всем клиентам до следующего удачного скачивания
    test = subprocess.run(["dnsmasq", "--test", "--conf-dir=" + DNSMASQ_CONF_DIR],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if test.returncode != 0:
        _restore_text(path, old)
        raise RoutingError("dnsmasq отверг конфиг списков (файл возвращён): "
                           + (test.stderr or test.stdout).decode(errors="replace").strip()[-300:])

    proc = subprocess.run(
        ["systemctl", "restart", config.ROUTING_DNSMASQ_SERVICE],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if proc.returncode != 0:
        raise RoutingError(
            "Не перезапустить dnsmasq: " + proc.stderr.decode(errors="replace").strip())
    return True


DNSMASQ_CONF_DIR = "/etc/dnsmasq.d,.dpkg-dist,.dpkg-old,.dpkg-new"


def _restore_text(path: str, old) -> None:
    """Вернуть прежний файл после отказа проверки (None — файла не было)."""
    try:
        if old is None:
            os.unlink(path)
        else:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                f.write(old)
            os.replace(path + ".tmp", path)
    except OSError as e:
        log.warning("routing: прежний конфиг списков не возвращён: %s", e)
