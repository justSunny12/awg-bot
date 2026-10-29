"""
probe.py — часть пакета quorum: формат пробы и ответа, разбор, потолки.

Датаграмма JSON внутри туннеля зонда на порт ответчика (`timing.port`):

    проба  {"q":1, "l":"a", "g":4, "n":"a1", "e":12, "w":"see", "s":48213}
    ответ  {"q":1, "l":"a", "g":4, "e":12, "r":"lead", "s":48213, "y":2, "v":3,
            "i":{"hs":37, "flow":true, "at":"2026-11-02T14:07:10"}}

`n` — узел; ответчик сверяет его с адресом источника (`probenet.node_of_addr`),
иначе пробу не считает. `w` — взгляд голосующего **до** этой пробы. `e` —
известная ему эпоха. `s` — номер пробы для сопоставления ответа. `r` — роль
хоста: `lead`, `fenced`, `follow`. `y`, `v` — счёт лидера; `i` — справка:
устройств с рукопожатием за 5 мин, рос ли приём за минуту, когда снята.
Мусор, лишние байты, незнакомый `q` — None: подписи поверх туннеля нет,
подделка без ключа зонда невозможна, повтор отсекает AWG.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import ROLES, VIEWS
from .members import SIDES, parse_node

MAX_DGRAM = 512
Q = 1
_MAX_S = 2 ** 31 - 1
_MAX_AT = 32


def _int(v, lo: int, hi: int) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        return None
    return v


@dataclass(frozen=True)
class Probe:
    l: str
    g: int
    n: str
    e: int
    w: str
    s: int

    def encode(self) -> bytes:
        return json.dumps({"q": Q, "l": self.l, "g": int(self.g), "n": self.n, "e": int(self.e),
                           "w": self.w, "s": int(self.s)}, separators=(",", ":")).encode("ascii")

    @classmethod
    def parse(cls, data: bytes) -> "Probe | None":
        d = _load(data)
        if d is None:
            return None
        try:
            l, n, w = str(d.get("l")), str(d.get("n")), str(d.get("w"))
            g, e, s = _int(d.get("g"), 1, _MAX_S), _int(d.get("e"), 0, _MAX_S), _int(d.get("s"), 0, _MAX_S)
            if l not in SIDES or w not in VIEWS or g is None or e is None or s is None:
                return None
            parse_node(n)
        except (ValueError, TypeError, AttributeError):
            return None
        return cls(l, g, n, e, w, s)


@dataclass(frozen=True)
class Info:
    """Справка ответчика: устройств с рукопожатием за 5 мин (без зондов), идёт ли
    трафик, когда снята (ISO, по часам ответчика)."""
    hs: int = 0
    flow: bool = False
    at: str = ""

    def to_dict(self) -> dict:
        return {"hs": int(self.hs), "flow": bool(self.flow), "at": str(self.at)[:_MAX_AT]}

    @classmethod
    def from_dict(cls, d) -> "Info":
        if not isinstance(d, dict):
            return cls()
        hs = _int(d.get("hs"), 0, 10 ** 6)
        return cls(hs=hs or 0, flow=bool(d.get("flow")), at=str(d.get("at") or "")[:_MAX_AT])


@dataclass(frozen=True)
class Reply:
    l: str
    g: int
    e: int
    r: str
    s: int
    y: int
    v: int
    i: Info = field(default_factory=Info)

    def encode(self) -> bytes:
        return json.dumps({"q": Q, "l": self.l, "g": int(self.g), "e": int(self.e), "r": self.r, "s": int(self.s),
                           "y": int(self.y), "v": int(self.v), "i": self.i.to_dict()},
                          separators=(",", ":"), ensure_ascii=True).encode("ascii")

    @classmethod
    def parse(cls, data: bytes) -> "Reply | None":
        d = _load(data)
        if d is None:
            return None
        l, r = str(d.get("l")), str(d.get("r"))
        g, e, s = _int(d.get("g"), 1, _MAX_S), _int(d.get("e"), 0, _MAX_S), _int(d.get("s"), 0, _MAX_S)
        y, v = _int(d.get("y"), 0, 255), _int(d.get("v"), 0, 255)
        if l not in SIDES or r not in ROLES or None in (g, e, s, y, v):
            return None
        return cls(l, g, e, r, s, y, v, Info.from_dict(d.get("i")))


def _load(data: bytes) -> dict | None:
    if not data or len(data) > MAX_DGRAM:
        return None
    try:
        d = json.loads(bytes(data).decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(d, dict) or d.get("q") != Q:
        return None
    return d
