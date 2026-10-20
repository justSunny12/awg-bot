"""
shamir.py — разделение секрета «2 из n» по Шамиру над GF(2⁸), побайтно; обе роли.

Прямая `f(x) = s ⊕ a·x` над полем GF(2⁸) (многочлен AES, 0x11B), отдельно для
каждого байта секрета: `s` — байт секрета, `a` — случайный байт наклона.
Точка держателя — `(x, f(x))`, x ∈ 1…255. Любые две точки с разными x дают и
секрет, и наклон; одна точка о секрете не говорит ничего: при неизвестном `a`
любое значение `s` равновероятно.

Здесь только арифметика и геометрия прямой. Кто на какой точке сидит
(резерв — 1, шлюзы — по слотам, аварийный код — 255), поколения и ключи —
`util/rsvkeys.py`.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

_POLY = 0x11B
_EXP = [0] * 512
_LOG = [0] * 256


def _tables() -> None:
    # образующая — 3: умножение на (x + 1), как в таблицах AES
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x ^= x << 1
        if x & 0x100:
            x ^= _POLY
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_tables()


def mul(a: int, b: int) -> int:
    """Произведение в GF(2⁸)."""
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def div(a: int, b: int) -> int:
    """Частное в GF(2⁸); делить на 0 нельзя."""
    if b == 0:
        raise ZeroDivisionError("деление на ноль в GF(2⁸)")
    if a == 0:
        return 0
    return _EXP[(_LOG[a] - _LOG[b]) % 255]


Point = tuple[int, bytes]   # (x, f(x))


def _check_x(x: int) -> int:
    x = int(x)
    if not 1 <= x <= 255:
        raise ValueError("точка Шамира — x от 1 до 255")
    return x


@dataclass(frozen=True)
class Line:
    """Прямая поколения: секрет и наклон. Держит только тот, кто раздаёт точки."""
    secret: bytes
    slope: bytes

    def __post_init__(self) -> None:
        if not self.secret or len(self.secret) != len(self.slope):
            raise ValueError("секрет и наклон — непустые строки байт одной длины")

    @classmethod
    def new(cls, secret: bytes) -> "Line":
        """Новая прямая через секрет со случайным наклоном."""
        if not secret:
            raise ValueError("секрет пуст")
        return cls(bytes(secret), secrets.token_bytes(len(secret)))

    def point(self, x: int) -> Point:
        """Точка держателя x."""
        x = _check_x(x)
        return x, bytes(s ^ mul(a, x) for s, a in zip(self.secret, self.slope))


def combine(p1: Point, p2: Point) -> Line:
    """Прямая по двум точкам с разными x; длины — одинаковые."""
    x1, y1 = _check_x(p1[0]), bytes(p1[1])
    x2, y2 = _check_x(p2[0]), bytes(p2[1])
    if x1 == x2:
        raise ValueError("две точки с одним x прямую не задают")
    if not y1 or len(y1) != len(y2):
        raise ValueError("точки разной длины — это не одна прямая")
    dx = x1 ^ x2
    slope = bytes(div(a ^ b, dx) for a, b in zip(y1, y2))
    secret = bytes(y ^ mul(a, x1) for y, a in zip(y1, slope))
    return Line(secret, slope)


def recover(p1: Point, p2: Point) -> bytes:
    """Секрет по двум точкам."""
    return combine(p1, p2).secret
