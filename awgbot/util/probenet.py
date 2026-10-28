"""
probenet.py — блок адресов пиров-зондов кворума резерва: 169.254.88.0/24.

Кворум (4.2.0) кладёт на пользовательский интерфейс сервера служебные пиры-
зонды голосующих (по одному адресу /32 из блока) и второй адрес интерфейса
из этого блока — отдельной строкой `Address` после основной. Пользовательскими
устройствами они не являются: сверка пиров их не трогает (ни «неизвестный»,
ни «пропавший»), опрос трафика не считает в онлайн, часть VPN пакета переезда
их не везёт и адрес интерфейса из блока не берёт. Этот модуль — с 4.1.0,
чтобы откат с 4.2.0 не заводил зонды в карантин с тревогой. Блок — не
настройка: его знают обе версии кода.

Зонд — пир, у которого **все** значения `AllowedIPs` лежат в блоке: пир с
адресом блока и ещё чем-то (адрес пользователей, `0.0.0.0/0`) — чужой, сторож
его ловит. Подсеть пользователей, пересекающая блок (экзотический префикс
`169.254.88`), правило выключает целиком: устройства там — настоящие.

Один источник подсетей пользователей для всех потребителей — строки `Address`
секции [Interface] живого конфига (`iface_addrs`): сверка, опрос трафика и
часть VPN берут их здесь, а не из настроек (`routing.client_subnet`) и не из
первого адреса строки. «Правило включено» (`rule_on`) считается один раз на
интерфейс, не на пира.
"""
from __future__ import annotations

import ipaddress
import re

PROBE_NET = ipaddress.ip_network("169.254.88.0/24")
_ADDRESS = re.compile(r"^[ \t]*Address[ \t]*=[ \t]*(.*)$", re.MULTILINE)


def _value(v: str) -> str:
    """Значение конфига без комментария в конце строки (`… # кворум`) и пробелов."""
    return str(v or "").split("#", 1)[0].strip()


def is_probe(ip: str) -> bool:
    """«169.254.88.5», «169.254.88.5/32», «169.254.88.1/26» — в блоке; мусор и IPv6 — нет."""
    try:
        addr = ipaddress.ip_interface(_value(ip)).ip
    except ValueError:
        return False
    return addr.version == 4 and addr in PROBE_NET


def _route_in_block(v: str) -> bool:
    """Значение AllowedIPs пира — маршрут, а не адрес: в блоке, только если вся его сеть
    внутри блока (169.254.88.9/0 — маршрут на весь интернет, не зонд)."""
    try:
        net = ipaddress.ip_network(_value(v), strict=False)
    except ValueError:
        return False
    return net.version == 4 and net.subnet_of(PROBE_NET)


def user_nets(addrs: list[str]) -> list[str]:
    """Подсети пользователей интерфейса по его адресам: адреса вне блока; если все в
    блоке — это и есть подсеть пользователей (правило зондов тогда не действует)."""
    vals = values(addrs)
    out = [a for a in vals if not is_probe(a)]
    return out or vals


def values(lines: list[str] | str) -> list[str]:
    """Значения через запятую из одной или нескольких строк (без комментариев)."""
    if isinstance(lines, str):
        lines = [lines]
    return [v.strip() for line in lines for v in _value(line).split(",") if v.strip()]


def overlaps(cidrs: list[str]) -> bool:
    """Подсеть пользователей (любая из cidrs) пересекает блок — правило зондов не действует."""
    for c in cidrs:
        try:
            net = ipaddress.ip_interface(_value(c)).network
        except ValueError:
            continue
        if net.version == 4 and net.overlaps(PROBE_NET):
            return True
    return False


def iface_addrs(conf_text: str) -> list[str]:
    """Значения всех строк `Address = …` конфига интерфейса (их бывает несколько,
    в каждой — через запятую), без комментариев; у пиров строк Address нет, так
    что годится и заголовок, и конфиг целиком."""
    return values([m.group(1) for m in _ADDRESS.finditer(str(conf_text or ""))])


def primary_addr(addrs: list[str]) -> str:
    """Адрес интерфейса для подсети пользователей: первый не из блока зондов; все в
    блоке — первый (это и есть подсеть пользователей); пусто — ''."""
    return next((a for a in addrs if not is_probe(a)), addrs[0] if addrs else "")


def rule_on(user_nets: list[str]) -> bool:
    """Правило зондов действует на интерфейсе: ни одна подсеть пользователей блок не
    пересекает. Считать один раз на интерфейс."""
    return not overlaps(list(user_nets or []))


def in_block(allowed: list[str] | str) -> bool:
    """Все значения AllowedIPs пира (строки или через запятую) — маршруты внутри блока,
    и они есть. Зонд — это `in_block` при включённом правиле (`rule_on`)."""
    vals = values(allowed)
    return bool(vals) and all(_route_in_block(v) for v in vals)


def peer_is_probe(allowed: list[str] | str, user_nets: list[str] | None = None) -> bool:
    """Пир с этими AllowedIPs (строки или значения через запятую) — зонд: значения
    есть и все в блоке, а подсеть пользователей блок не пересекает."""
    return in_block(allowed) and rule_on(list(user_nets or []))


def line_net(side: str) -> ipaddress.IPv4Network:
    """Подблок линии: «a» (основа пары) — .0/26, «b» (вторая сторона) — .64/26."""
    if side == "a":
        return ipaddress.ip_network("169.254.88.0/26")
    if side == "b":
        return ipaddress.ip_network("169.254.88.64/26")
    raise ValueError(f"сторона линии — a или b, не {side!r}")
