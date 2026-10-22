"""
dnsapi — A-запись адреса для клиентов: чтение и смена у провайдера; основной бот.

Запись `app.network.server_host` — арбитр VPN: куда она указывает, туда идут
клиенты. Меняет её основа (кнопка, возврат) или резерв (авария, токеном из
конверта под `M`). Факт смены проверяется не ответом API, а чтением: у
провайдера через API и у авторитетного сервера зоны напрямую (`common`).

Провайдеры: Cloudflare (`cloudflare`), DuckDNS (`duckdns`), протокол DynDNS2 —
No-IP, Dynu, OVH DynHost, deSEC, Hurricane Electric и подобные (`dyndns2`),
DigitalPlat DNS — хостинг кабинета DigitalPlat One (`digitalplat`), и
«вручную» (`manual`): запись меняет человек, автоматический переезд
невозможен.

Настройка провайдера — словарь `{provider, name, token, ...}` (хранится в
`server_state` ключом `rsv_dns`, секрет — токен). Все вызовы синхронные
(`urllib`), бот зовёт их из потока; сеть ограничена таймаутами.
"""
from __future__ import annotations

from .common import (DnsError, NotSupported, Provider, provider_for, read_authoritative, read_authoritative_ttl,
                     read_system, PROVIDERS)

__all__ = ["DnsError", "NotSupported", "Provider", "provider_for", "read_authoritative", "read_authoritative_ttl",
           "read_system", "PROVIDERS"]
