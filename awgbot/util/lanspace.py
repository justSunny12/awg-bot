"""
lanspace.py — адреса в туннеле для локальных сетей шлюзов: служебное
пространство сервера, префикс слота, двойник подсети и адреса, обратный
перевод, проверки ввода и самого пространства.

Пространство (routing.lan_space, по умолчанию 10.240.0.0/12) делится на
шестнадцать /16: слоты 1–15 и последняя — служебные адреса сервера (резолвер
админа, адрес перехвата DNS). У подсети S слота N — двойник той же длины:

    префикс слота  P(N)    = начало пространства + (N − 1) · 65536, /16
    двойник        T(N, S) = P(N) | (адрес сети S & 0.0.255.255)
    адрес хоста    t(N, a) = P(N) | (a & 0.0.255.255)

Две последние цифры адреса сохраняются: 192.168.1.10 за слотом 1 —
10.240.1.10. Пределы: слоты 1–15; подсеть не шире /16; двойники подсетей
одного слота не пересекаются (с вложенностью: 192.168.0.0/22 и 10.0.2.0/24
дают 10.240.0.0/22 ⊃ 10.240.2.0/24); подсеть не внутри пространства.

Модуль общий для ролей: адреса вычисляет сервер, агент только показывает,
и показ с переводом не должны разойтись. Чистые функции: хост не читается,
из конфигурации — только строка routing.lan_space.
"""
from __future__ import annotations

import functools
import ipaddress
import logging
from typing import Iterable, Mapping, Optional, Union

log = logging.getLogger(__name__)

DEFAULT_SPACE = "10.240.0.0/12"
SPACE_PREFIXLEN = 12
SLOT_PREFIXLEN = 16             # на слот в туннеле — /16: шире подсеть не влезет
MAX_SLOTS = 15                  # шестнадцатая /16 пространства — служебная
SERVICE_NET = "10.255.0.0/16"   # служебная /16 пространства по умолчанию
_HOST_MASK = 0xFFFF             # хостовая часть /16 — она и переносится в двойник
# Частные диапазоны — те же, что пускают правила AllowedIPs шлюза
# (install/gw-wg-allow.list) и проверка документа конфигурации.
_RFC1918 = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))

Net = ipaddress.IPv4Network
Addr = ipaddress.IPv4Address
NetLike = Union[str, Net]
AddrLike = Union[str, Addr]


# ─────────────────────────────────────────────────────────────────────────────
# Пространство
# ─────────────────────────────────────────────────────────────────────────────

def parse_space(raw) -> tuple[Net, str]:
    """(пространство, причина отказа). Пусто — умолчание без причины; годится
    только частная сеть /12 без хостовых битов; иначе — умолчание и причина."""
    default = ipaddress.IPv4Network(DEFAULT_SPACE)
    text = str(raw or "").strip()
    if not text:
        return default, ""
    try:
        net = ipaddress.IPv4Network(text)
    except ValueError:
        return default, f"«{text}» — не сеть IPv4 (или с хостовыми битами)"
    if net.prefixlen != SPACE_PREFIXLEN:
        return default, f"{net} — нужна /{SPACE_PREFIXLEN}: по /{SLOT_PREFIXLEN} на {MAX_SLOTS} слотов и служебная"
    if not any(net.subnet_of(n) for n in _RFC1918):
        return default, f"{net} — нужна частная сеть (10/8 или 172.16/12)"
    return net, ""


@functools.lru_cache(maxsize=8)
def _space_from(raw: str) -> Net:
    net, why = parse_space(raw)
    if why:
        log.warning("routing.lan_space: %s, беру %s", why, DEFAULT_SPACE)
    return net


def space() -> Net:
    """Действующее пространство из routing.lan_space (conf/app.yaml); неверное
    значение — умолчание с записью в журнал (один раз на значение)."""
    from awgbot.core import config
    return _space_from(getattr(config, "ROUTING_LAN_SPACE", "") or "")


def _sp(space_net: Optional[NetLike]) -> Net:
    return space() if space_net is None else ipaddress.IPv4Network(str(space_net))


def service_net(space_net: Optional[NetLike] = None) -> Net:
    """Служебная /16 — последняя в пространстве (по умолчанию 10.255.0.0/16)."""
    sp = _sp(space_net)
    return list(sp.subnets(new_prefix=SLOT_PREFIXLEN))[-1]


# ─────────────────────────────────────────────────────────────────────────────
# Перевод
# ─────────────────────────────────────────────────────────────────────────────

def slot_prefix(slot_id: int, space_net: Optional[NetLike] = None) -> Net:
    """P(N) — /16 слота. ValueError — номер вне 1..MAX_SLOTS."""
    n = int(slot_id)
    if not 1 <= n <= MAX_SLOTS:
        raise ValueError(f"слот {n}: адреса в туннеле есть только у слотов 1–{MAX_SLOTS}")
    base = int(_sp(space_net).network_address) + (n - 1) * (_HOST_MASK + 1)
    return ipaddress.IPv4Network((base, SLOT_PREFIXLEN))


def _net(net: NetLike) -> Net:
    return net if isinstance(net, ipaddress.IPv4Network) else ipaddress.IPv4Network(str(net).strip(), strict=False)


def too_wide(net: NetLike) -> bool:
    """Подсеть шире /16 — на слот в туннеле приходится /16, не влезет."""
    return _net(net).prefixlen < SLOT_PREFIXLEN


def twin_net(slot_id: int, net: NetLike, space_net: Optional[NetLike] = None) -> Net:
    """T(N, S) — двойник подсети в туннеле, той же длины. ValueError — слот
    вне пределов или подсеть шире /16."""
    s = _net(net)
    if too_wide(s):
        raise ValueError(f"{s} шире /{SLOT_PREFIXLEN}")
    p = slot_prefix(slot_id, space_net)
    return ipaddress.IPv4Network((int(p.network_address) | (int(s.network_address) & _HOST_MASK), s.prefixlen))


def twin_addr(slot_id: int, addr: AddrLike, space_net: Optional[NetLike] = None) -> Addr:
    """t(N, a) — адрес хоста в туннеле."""
    a = ipaddress.IPv4Address(str(addr).strip())
    return ipaddress.IPv4Address(int(slot_prefix(slot_id, space_net).network_address) | (int(a) & _HOST_MASK))


def slot_of(twin: Union[AddrLike, NetLike], space_net: Optional[NetLike] = None) -> Optional[int]:
    """Номер слота, чей префикс содержит адрес или сеть в туннеле; None — вне
    префиксов слотов (вне пространства или в служебной /16)."""
    try:
        t = ipaddress.IPv4Network(str(twin).strip(), strict=False)
    except ValueError:
        return None
    sp = _sp(space_net)
    if t.prefixlen < SLOT_PREFIXLEN or not t.subnet_of(sp):
        return None
    n = ((int(t.network_address) - int(sp.network_address)) >> SLOT_PREFIXLEN) + 1
    return n if n <= MAX_SLOTS else None


def real_addr(twin: AddrLike, slots: Mapping[int, Iterable[str]],
              space_net: Optional[NetLike] = None) -> Optional[Addr]:
    """Обратный перевод для показа: адрес в туннеле → настоящий адрес в сети
    слота. slots — {номер слота: настоящие подсети}. None — слота нет, или
    адрес не попадает ни в один двойник его подсетей."""
    try:
        t = ipaddress.IPv4Address(str(twin).strip())
    except ValueError:
        return None
    n = slot_of(t, space_net)
    if n is None:
        return None
    for raw in slots.get(n) or ():
        try:
            s = _net(raw)
            if t in twin_net(n, s, space_net):
                return ipaddress.IPv4Address((int(s.network_address) & ~_HOST_MASK) | (int(t) & _HOST_MASK))
        except ValueError:
            continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Проверки ввода
# ─────────────────────────────────────────────────────────────────────────────

def in_space(net: NetLike, space_net: Optional[NetLike] = None) -> bool:
    """Подсеть задевает служебное пространство — на шлюзе её адреса совпали бы
    с адресами соседей в его AllowedIPs."""
    return _net(net).overlaps(_sp(space_net))


def twin_clash(slot_id: int, net: NetLike, others: Iterable[str],
               space_net: Optional[NetLike] = None) -> str:
    """Первая из others (саму net туда не класть), чей двойник пересекается — в
    том числе вложенностью — с двойником net в том же слоте; "" — пересечений
    нет. Подсети шире /16 и нечитаемые — мимо: у них двойника нет, отказ им —
    по своей причине."""
    try:
        t = twin_net(slot_id, net, space_net)
    except ValueError:
        return ""
    for raw in others:
        try:
            o = _net(raw)
            if t.overlaps(twin_net(slot_id, o, space_net)):
                return str(o)
        except ValueError:
            continue
    return ""


def twin_clashes(slot_id: int, nets: Iterable[str],
                 space_net: Optional[NetLike] = None) -> list[tuple[str, str]]:
    """[(подсеть, с какой пересеклась)] по порядку списка: подсеть, чей двойник
    пересекается с двойником одной из прежних (не отброшенных), отбрасывается —
    видна только первая."""
    kept: list[str] = []
    out: list[tuple[str, str]] = []
    for raw in nets:
        other = twin_clash(slot_id, raw, kept, space_net)
        if other:
            out.append((str(_net(raw)), other))
        else:
            kept.append(str(raw))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Проверка пространства
# ─────────────────────────────────────────────────────────────────────────────

def space_problem(space_net: NetLike, client_subnets: Iterable[str], link_nets: Iterable[str],
                  admin_dns: str = "", intercept_addr: str = "") -> str:
    """Причина, по которой пространство нельзя включить, или "". Пространство не
    пересекается с подсетями клиентов (вместе с подсетью переезда) и с /30
    линков; резолвер админа и адрес перехвата DNS — в служебной /16
    пространства или вне его. Пустые и нечитаемые входы — мимо."""
    sp = ipaddress.IPv4Network(str(space_net))
    svc = service_net(sp)
    for what, nets in (("подсетью клиентов", client_subnets), ("подсетью линка", link_nets)):
        for raw in nets or ():
            try:
                n = _net(raw)
            except ValueError:
                continue
            if n.overlaps(sp):
                return f"служебное пространство {sp} пересекается с {what} {n}"
    for what, raw in (("резолвер админа", admin_dns), ("адрес перехвата DNS", intercept_addr)):
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            a = ipaddress.IPv4Address(text)
        except ValueError:
            continue
        if a in sp and a not in svc:
            return f"{what} {a} внутри пространства {sp}, но вне служебных адресов {svc}"
    return ""


__all__ = ["DEFAULT_SPACE", "MAX_SLOTS", "SERVICE_NET", "SLOT_PREFIXLEN", "parse_space", "space",
           "service_net", "slot_prefix", "too_wide", "twin_net", "twin_addr", "slot_of", "real_addr",
           "in_space", "twin_clash", "twin_clashes", "space_problem"]
