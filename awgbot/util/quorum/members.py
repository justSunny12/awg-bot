"""
members.py — часть пакета quorum: состав линии, поколение, порог, окно
прежнего поколения.

Состав линии `l` ведёт её основа: сама, второй сервер и свои шлюзы с
возможностью `quo1`; поколение `g` растёт на каждой смене и действует с
подтверждения второго сервера. Голосующие о хосте X — ансамбль без X;
v = n − 1; порог t = ⌊v/2⌋ + 1 — строгое большинство. Кворум линии включён
при v ≥ 2 (есть хотя бы один шлюз `quo1`), иначе линия на матрице (кнопка и
код). Окно прежнего поколения: после смены состава голос узла, который есть
в обоих составах, считается ещё `grace_s` под прежним `g`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from awgbot.util import probenet

_NODE = re.compile(r"^([ab])([1-9][0-9]?)?$")
SIDES = ("a", "b")


def parse_node(node: str) -> tuple[str, int]:
    """«a» → (a, 0) — сервер; «a12» → (a, 12) — шлюз; мусор или слот выше
    `probenet.MAX_SLOT` — ValueError."""
    m = _NODE.match(str(node or ""))
    if not m:
        raise ValueError(f"узел — a, b или сторона и слот (a1), не {node!r}")
    slot = int(m.group(2) or 0)
    if slot > probenet.MAX_SLOT:
        raise ValueError(f"слот {slot} в голосовании не участвует (до {probenet.MAX_SLOT})")
    return m.group(1), slot


def is_server(node: str) -> bool:
    return node in SIDES


def gateway_node(side: str, slot: int) -> str:
    if side not in SIDES:
        raise ValueError(f"сторона — a или b, не {side!r}")
    return f"{side}{int(slot)}"


def threshold(v: int) -> int:
    """Порог — строгое большинство от v."""
    return int(v) // 2 + 1


def majority(k: int, v: int) -> bool:
    """k — строго больше половины v («нет» для переезда, «да» Q2 для возврата)."""
    return 2 * int(k) > int(v)


def half_or_more(k: int, v: int) -> bool:
    """k не меньше половины v (лидер держится)."""
    return 2 * int(k) >= int(v)


@dataclass(frozen=True)
class Members:
    """Состав линии: сторона, поколение, узлы. Узлы — оба сервера и шлюзы стороны линии."""
    l: str
    g: int
    members: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.l not in SIDES:
            raise ValueError(f"линия — a или b, не {self.l!r}")
        if int(self.g) < 1:
            raise ValueError("поколение состава — от 1")
        seen = set()
        for n in self.members:
            side, _ = parse_node(n)
            if n in seen:
                raise ValueError(f"узел {n} в составе дважды")
            if not is_server(n) and side != self.l:
                raise ValueError(f"шлюз {n} другой стороны в линии {self.l} не голосует")
            seen.add(n)
        if self.l not in seen or probenet.other_side(self.l) not in seen:
            raise ValueError("в составе должны быть оба сервера")

    @classmethod
    def make(cls, l: str, g: int, members) -> "Members":
        return cls(str(l), int(g), tuple(str(n) for n in members))

    @classmethod
    def from_dict(cls, d: dict) -> "Members":
        return cls.make(d.get("l", ""), d.get("g", 0), d.get("members") or [])

    def to_dict(self) -> dict:
        return {"l": self.l, "g": int(self.g), "members": list(self.members)}

    @property
    def n(self) -> int:
        return len(self.members)

    @property
    def gateways(self) -> tuple[str, ...]:
        return tuple(m for m in self.members if not is_server(m))

    def voters(self, leader: str) -> tuple[str, ...]:
        """Голосующие о хосте-лидере: ансамбль без него. Лидер — `l` (основа у
        себя) или другой сервер (бот основы на резерве)."""
        if leader not in self.members:
            raise ValueError(f"лидер {leader!r} не в составе")
        return tuple(m for m in self.members if m != leader)

    def v(self, leader: str) -> int:
        return len(self.voters(leader))

    def t(self, leader: str) -> int:
        return threshold(self.v(leader))

    @property
    def enabled(self) -> bool:
        """Кворум линии возможен: хотя бы один шлюз `quo1` (v ≥ 2)."""
        return bool(self.gateways)

    def with_gateway(self, node: str) -> "Members":
        if node in self.members:
            return self
        return Members(self.l, self.g + 1, self.members + (str(node),))

    def without_gateway(self, node: str) -> "Members":
        if node not in self.members or is_server(node):
            return self
        return Members(self.l, self.g + 1, tuple(m for m in self.members if m != node))


@dataclass
class GenWindow:
    """Действующее поколение и окно прежнего: после смены `grace_s` голос с
    прежним `g` ещё в счёте."""
    g: int = 0
    prev_g: int = 0
    until: float = 0.0

    def bump(self, g: int, now: float, grace_s: float) -> None:
        g = int(g)
        if g == self.g:
            return
        self.prev_g, self.g, self.until = self.g, g, now + float(grace_s)

    def accepts(self, g: int, now: float) -> bool:
        g = int(g)
        if g == self.g:
            return True
        return g == self.prev_g and self.prev_g > 0 and now < self.until


@dataclass
class Composition:
    """Состав линии с подтверждением: `acked` — поколение, подтверждённое вторым
    сервером; действует `active()` — последнее подтверждённое."""
    mine: Members
    acked: int = 0
    history: dict = field(default_factory=dict)      # g → Members (подтверждённые и ждущие)

    def active(self) -> Members | None:
        m = self.history.get(int(self.acked))
        if m is None and int(self.acked) == int(self.mine.g):
            return self.mine
        return m

    def pending(self) -> bool:
        return int(self.mine.g) != int(self.acked)

    def propose(self, m: Members) -> None:
        if int(self.mine.g) == int(self.acked) and int(self.acked) not in self.history:
            self.history[int(self.acked)] = self.mine          # действующий состав (поднят с диска) — остаётся
        self.history[int(m.g)] = m
        self.mine = m

    def ack(self, g: int) -> bool:
        """Подтверждение второго сервера; поколение должно быть предложенным."""
        g = int(g)
        if g != int(self.mine.g):
            return False
        self.acked = g
        self.history[g] = self.mine
        return True


def off_reasons(m: Members | None, *, peer_quo1: bool, acked: bool, timing_problem: str = "",
                overlaps: list[str] | None = None, big_slots: list[int] | None = None,
                keys_ok: bool = True) -> list[str]:
    """Почему кворум линии не включён — кодами (тексты — у бота): пусто — включён."""
    out: list[str] = []
    if not peer_quo1:
        out.append("peer_old")
    if m is None or not m.enabled:
        out.append("no_gateways")
    if not acked:
        out.append("not_acked")
    if not keys_ok:
        out.append("keys")
    if timing_problem:
        out.append("timing:" + timing_problem)
    for net in overlaps or []:
        out.append("overlap:" + str(net))
    for slot in big_slots or []:
        out.append(f"slot:{int(slot)}")
    return out
