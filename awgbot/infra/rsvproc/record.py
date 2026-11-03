"""record.py — часть пакета rsvproc (см. __init__): исход смены записи DNS неизвестен
(запрос к провайдеру кончился исключением — таймаут, ответ не дошёл или не разобран) —
факт у авторитетного сервера зоны вместо ответа API. Токена провайдера здесь нет: только
чтение записи мимо кэшей (dnsapi.read_authoritative, таймауты запросов ≤ 3 с).

Та же логика у бота резерва, когда процесс открытия погиб посреди неизвестного
(domain/services/reserve/inbound_recwait): первое чтение сразу, дальше раз в
FACT_RETRY_S ± 40 % до предела от шага `record_try`, последнее — на самом пределе."""
from __future__ import annotations

import time

FACT_RETRY_S = 60                 # авторитетный ответ при неизвестном исходе — раз в минуту ± 40 %


def auth_has(name: str, ip: str) -> bool:
    """`ip` среди A-адресов `name` у авторитетного сервера; не прочиталось — нет (факта нет)."""
    from awgbot.util import dnsapi
    try:
        return ip in dnsapi.read_authoritative(name)
    except dnsapi.DnsError:
        return False


def wait_unknown(name: str, ip: str, deadline: float) -> bool:
    """Ждать факта до `deadline` (time.monotonic): чтение раз в FACT_RETRY_S ± 40 %, последнее —
    на самом сроке. True — адрес у авторитетного сервера мой."""
    from awgbot.util import backoff
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(left, backoff.jitter(FACT_RETRY_S)))
        if auth_has(name, ip):
            return True
