"""
wgconf.py — конфиги WireGuard, которые ездят каналом ВПС ↔ шлюз: разбор по
секциям, белые списки ключей, правило AllowedIPs линка, канонический текст.

Один источник для обеих ролей и для рубежа в sh: белые списки и пределы
правила лежат данными в install/gw-wg-allow.list — его читают этот модуль
(сборка желаемого конфига на ВПС, проверка документа на агенте) и
`wg_conf_check` скрипта обвязки. Расхождение «что собрал сервер» и «что
примет шлюз» тогда невозможно по построению, а не ловится тестом.

Почему белый список, а не чёрный: awg-quick исполняет PreUp/PostUp/PreDown/
PostDown от root, DNS перепишет резолвер системы, Table у линка увёл бы весь
трафик шлюза в туннель. Конфиг из канала — данные, и принимается в нём ровно
то, что названо.

Правило AllowedIPs линка (строго): записи — только из того же документа:
подсеть клиентов, /30 линка, подсети других шлюзов и /32 резолвера; все —
RFC 1918, префикс не короче /16, не больше allowed_max записей, без
пересечений с локальными подсетями шлюза и подсетями его интерфейса.
"""
from __future__ import annotations

import ipaddress
import re
from pathlib import Path

ALLOW_LIST = Path(__file__).resolve().parents[2] / "install" / "gw-wg-allow.list"
KINDS = ("link", "uplink")
SECTIONS = ("Interface", "Peer")
STAMP_RE = re.compile(r"^#\s*awg-bot:\s*контракт линка\s+(\d+)\s*$")
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
_B64_32_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")


class WgConfError(ValueError):
    """Конфиг не принят: ключ вне белого списка, не та структура, правило AllowedIPs."""


def norm(val) -> str:
    """Нормализация значения настроек: пробелы схлопнуты, None — пусто."""
    return " ".join(str(val if val is not None else "").split())


# ── белые списки и пределы — из данных ───────────────────────────────────────

class Allow:
    """Содержимое gw-wg-allow.list: keys[kind][section] — ключи по порядку файла;
    allowed_max, allowed_min_prefix, allowed_nets — пределы правила."""

    def __init__(self, text: str):
        self.keys: dict[str, dict[str, list[str]]] = {k: {s: [] for s in SECTIONS} for k in KINDS}
        self.allowed_max = 0
        self.allowed_min_prefix = 0
        self.allowed_nets: list[ipaddress.IPv4Network] = []
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if parts[0] == "allowed_max" and len(parts) == 2:
                self.allowed_max = int(parts[1])
            elif parts[0] == "allowed_min_prefix" and len(parts) == 2:
                self.allowed_min_prefix = int(parts[1])
            elif parts[0] == "allowed_nets":
                self.allowed_nets = [ipaddress.ip_network(n) for n in parts[1:]]
            elif len(parts) == 3 and parts[0] in KINDS and parts[1] in SECTIONS and _KEY_RE.match(parts[2]):
                self.keys[parts[0]][parts[1]].append(parts[2])
            else:
                raise WgConfError(f"gw-wg-allow.list: непонятная строка «{raw.strip()[:60]}»")
        if not (self.allowed_max and self.allowed_min_prefix and self.allowed_nets):
            raise WgConfError("gw-wg-allow.list: не заданы пределы правила AllowedIPs")
        for kind in KINDS:
            if not self.keys[kind]["Interface"] or not self.keys[kind]["Peer"]:
                raise WgConfError(f"gw-wg-allow.list: пустой белый список {kind}")


_allow: Allow | None = None


def allow() -> Allow:
    """Белые списки из поставки; файла нет — закрыто: ни один конфиг не принимается."""
    global _allow
    if _allow is None:
        try:
            _allow = Allow(ALLOW_LIST.read_text(encoding="utf-8"))
        except OSError as e:
            raise WgConfError(f"нет {ALLOW_LIST.name} в поставке — конфиги каналом не принимаются") from e
    return _allow


# ── разбор ───────────────────────────────────────────────────────────────────

class Conf:
    """Разобранный конфиг: sections — [(имя, [(ключ, значение)])] по порядку
    файла; stamp — номер контракта из комментария-штампа; comments — прочие
    комментарии (у линка из канала их быть не должно)."""

    def __init__(self):
        self.sections: list[tuple[str, list[tuple[str, str]]]] = []
        self.stamp = ""
        self.comments: list[str] = []

    def section(self, name: str) -> list[tuple[str, str]]:
        for n, items in self.sections:
            if n == name:
                return items
        return []

    def get(self, section: str, key: str) -> str:
        for k, v in self.section(section):
            if k.lower() == key.lower():
                return v
        return ""

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.sections if n == name)


def parse(text: str) -> Conf:
    """Разобрать текст конфига. Регистр ключей не важен (как у wg), значение —
    всё после первого «=», обрезанное. Строка вне секции или без «=» — отказ."""
    conf = Conf()
    cur: list[tuple[str, str]] | None = None
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = STAMP_RE.match(line)
            if m:
                conf.stamp = m.group(1)
            else:
                conf.comments.append(line)
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if name not in SECTIONS:
                raise WgConfError(f"неизвестная секция [{name[:20]}]")
            cur = []
            conf.sections.append((name, cur))
            continue
        if cur is None or "=" not in line:
            raise WgConfError(f"строка вне секции или без «=»: «{line[:40]}»")
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if not _KEY_RE.match(key):
            raise WgConfError(f"недопустимое имя ключа «{key[:20]}»")
        cur.append((key, val))
    return conf


def _canon_key(kind: str, section: str, key: str) -> str | None:
    for k in allow().keys[kind][section]:
        if k.lower() == key.lower():
            return k
    return None


def check_keys(conf: Conf, kind: str) -> None:
    """Белый список: каждый ключ каждой секции назван в gw-wg-allow.list;
    ровно одна [Interface] и ровно один [Peer]; у линка комментарий — только
    штамп контракта."""
    if kind not in KINDS:
        raise WgConfError(f"неизвестный вид конфига {kind}")
    if conf.count("Interface") != 1 or conf.count("Peer") != 1:
        raise WgConfError("нужны ровно одна секция [Interface] и один [Peer]")
    for section, items in conf.sections:
        seen: set[str] = set()
        for key, _val in items:
            canon = _canon_key(kind, section, key)
            if canon is None:
                raise WgConfError(f"[{section}] {key}: ключ вне белого списка")
            if canon in seen:
                raise WgConfError(f"[{section}] {canon}: ключ повторяется")
            seen.add(canon)
    if kind == "link" and conf.comments:
        raise WgConfError("в конфиге линка допустим только штамп контракта")


def canonical(conf: Conf, kind: str) -> str:
    """Канонический текст: секции в порядке [Interface], [Peer], ключи — в
    порядке белого списка, значения нормализованы; без комментариев и штампа.
    На нём считается отпечаток: пробелы, порядок ключей и штамп его не меняют."""
    out = []
    for section in SECTIONS:
        items = conf.section(section)
        out.append(f"[{section}]")
        for canon in allow().keys[kind][section]:
            for key, val in items:
                if key.lower() == canon.lower():
                    out.append(f"{canon} = {norm(val)}")
    return "\n".join(out) + "\n"


def render(conf: Conf, kind: str, stamp: str = "") -> str:
    """Текст конфига для записи на диск: как canonical, со штампом контракта
    первой строкой (у линка) — его читает снимок (gwsnapshot.link_contract)."""
    head = f"# awg-bot: контракт линка {stamp}\n" if stamp else ""
    body = canonical(conf, kind).replace("\n[Peer]", "\n\n[Peer]")
    return head + body


def set_value(conf: Conf, section: str, key: str, value: str) -> None:
    """Поставить значение ключа в секции (заменить или добавить)."""
    items = conf.section(section)
    if not items and not conf.count(section):
        items = []
        conf.sections.append((section, items))
    for i, (k, _v) in enumerate(items):
        if k.lower() == key.lower():
            items[i] = (k, value)
            return
    items.append((key, value))


# ── правило AllowedIPs линка ─────────────────────────────────────────────────

def _net(tok: str) -> ipaddress.IPv4Network:
    try:
        net = ipaddress.ip_network(tok, strict=True)
    except ValueError as e:
        raise WgConfError(f"AllowedIPs: «{tok[:40]}» — не подсеть ({e})") from e
    if net.version != 4:
        raise WgConfError(f"AllowedIPs: «{tok[:40]}» — только IPv4")
    return net


def split_list(val: str) -> list[str]:
    """Список через запятые и/или пробелы — в токены."""
    return [t for t in re.split(r"[,\s]+", str(val or "").strip()) if t]


def allowed_ips_want(*, client_subnet: str, link_cidr: str, peer_nets, resolver: str = "") -> list[str]:
    """Что обязано стоять в AllowedIPs линка по документу: подсеть клиентов,
    /30 линка, подсети других шлюзов, /32 резолвера (если задан)."""
    out = [client_subnet, link_cidr, *list(peer_nets or [])]
    if resolver:
        out.append(f"{resolver}/32")
    return out


def check_allowed_ips(entries, *, client_subnet: str, link_cidr: str, peer_nets, resolver: str = "",
                      lan_subnets=(), iface_nets=()) -> list[str]:
    """Правило AllowedIPs линка. Возвращает нормализованный список записей
    или бросает WgConfError с причиной. Строго: записи — ровно из документа
    (подсеть клиентов и /30 обязательны, подсети соседей — каждая, /32
    резолвера — если задан), все RFC 1918, префикс /allowed_min_prefix–/32, не
    больше allowed_max, без пересечений с локальными подсетями шлюза и
    подсетями его интерфейса."""
    a = allow()
    toks = split_list(entries) if isinstance(entries, str) else [str(t) for t in entries]
    if len(toks) > a.allowed_max:
        raise WgConfError(f"AllowedIPs: записей {len(toks)}, предел {a.allowed_max}")
    nets = [_net(t) for t in toks]
    for net in nets:
        if net.prefixlen < a.allowed_min_prefix:
            raise WgConfError(f"AllowedIPs: {net} шире /{a.allowed_min_prefix}")
        if not any(net.subnet_of(p) for p in a.allowed_nets):
            raise WgConfError(f"AllowedIPs: {net} вне частных диапазонов")
    want = [_net(t) for t in allowed_ips_want(client_subnet=client_subnet, link_cidr=link_cidr,
                                               peer_nets=peer_nets, resolver=resolver)]
    # обязательны подсеть клиентов и /30 линка; подсети соседей и /32 резолвера
    # правило допускает (только из документа), но не требует — сервер решает,
    # что класть, а шлюз — что это из того же документа
    for req in want[:2]:
        if req not in nets:
            raise WgConfError(f"AllowedIPs: нет обязательной записи {req}")
    for net in nets:
        if net not in want:
            raise WgConfError(f"AllowedIPs: {net} — записи нет в документе")
    for other in [*split_list(" ".join(lan_subnets)), *split_list(" ".join(iface_nets))]:
        try:
            o = ipaddress.ip_network(other, strict=False)
        except ValueError:
            continue
        for net in nets:
            if net.version == o.version and net.overlaps(o):
                raise WgConfError(f"AllowedIPs: {net} пересекается с подсетью шлюза {o}")
    return [str(n) for n in nets]


def is_pubkey(val: str) -> bool:
    """Ключ WireGuard: base64 от 32 байт."""
    return bool(_B64_32_RE.match(str(val or "")))
