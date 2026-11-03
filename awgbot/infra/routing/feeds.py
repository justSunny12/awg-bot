"""feeds.py — внешние списки и конфиг dnsmasq: загрузка, запрос к dnsmasq бота для досева, запись конфига."""

from __future__ import annotations

import os
import subprocess
from typing import Optional

from awgbot.core import config
from awgbot.infra import systemd
from awgbot.util import fsatomic, net4

from .base import log, RoutingError

# ─────────────────────────────────────────────────────────────────────────────
# Внешние списки и конфиг dnsmasq
# ─────────────────────────────────────────────────────────────────────────────

def fetch(url: str, timeout: int = 10) -> tuple[Optional[str], str, int]:
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
        with net4.urlopen(req, timeout=timeout) as resp:              # только IPv4
            return resp.read().decode("utf-8", errors="replace"), "", 200
    except urllib.error.HTTPError as e:
        log.warning("routing: не скачался %s (%s)", url, e)
        return None, (str(e) or e.__class__.__name__)[:200], int(e.code)
    except Exception as e:                                # noqa: BLE001
        log.warning("routing: не скачался %s (%s)", url, e)
        return None, (str(e) or e.__class__.__name__)[:200], 0


def ask_dnsmasq(server: str, domain: str, timeout: float) -> bool:
    """Задать dnsmasq бота A-запрос по домену. True — в ответе есть адреса.

    Досев набора: адреса в набор кладёт сам dnsmasq по своей директиве
    `ipset=/домен/vpn_uN` — она действует на любой ответ, и спросивший бот для
    неё ничем не отличается от устройства (man dnsmasq, --ipset: «Places the
    resolved IP addresses of queries for one or more domains in the specified
    Netfilter IP set»). Бот в набор ничего не пишет, поэтому набор совпадает
    с тем, что dnsmasq отдаёт устройствам.

    Цена лишнего адреса в наборе — «поедет на шлюз»: досев по домену на общем
    хостинге может утащить в канал шлюза чужой адрес. Эффект заперт в наборе
    одного профиля и снимается удалением домена из его списка — ровно как от
    запроса самого устройства.

    Свой DNS-клиент по UDP (dnsapi.common.query), порт 53, потолок — timeout:
    нет ответа, ошибка сервера, искажённый ответ — False, не исключение.
    """
    from awgbot.util.dnsapi import common as dns
    try:
        recs = dns.query(server, domain, 1, timeout)
    except dns.DnsError:
        return False
    return any(rtype == 1 for _name, rtype, *_rest in recs)


def write_dnsmasq_conf(text: str, path: str = None) -> bool:
    """Записать конфиг списков и применить. True — файл изменился (был рестарт).

    Дифф-скип обязателен: реконсиляция ходит по расписанию, а рестарт dnsmasq
    роняет кэш ВСЕМ пользователям. Перезапускать при неизменном содержимом — значит
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

    try:
        fsatomic.write_atomic(path, text)         # атомарно: без полуфайла; права прежнего файла
    except OSError as e:
        raise RoutingError(f"Не записать {path}: {e}")

    # Синтаксис — до рестарта, с conf-dir (голый --test файлы каталога не читает):
    # битая строка иначе роняла бы DNS всем пользователям до следующего удачного скачивания
    test = subprocess.run(["dnsmasq", "--test", "--conf-dir=" + DNSMASQ_CONF_DIR],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if test.returncode != 0:
        _restore_text(path, old)
        raise RoutingError("dnsmasq отверг конфиг списков (файл возвращён): "
                           + (test.stderr or test.stdout).decode(errors="replace").strip()[-300:])

    proc = systemd.restart(config.ROUTING_DNSMASQ_SERVICE, timeout=30)
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
            fsatomic.write_atomic(path, old)
    except OSError as e:
        log.warning("routing: прежний конфиг списков не возвращён: %s", e)
