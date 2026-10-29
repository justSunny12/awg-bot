"""
shamir.py — разделение секрета по Шамиру над GF(2⁸), побайтно; обе роли.

Многочлен `f(x) = s ⊕ a₁·x ⊕ … ⊕ a_{t−1}·x^{t−1}` над полем GF(2⁸) (многочлен
AES, 0x11B), отдельно для каждого байта секрета: `s` — байт секрета,
`a_j` — случайные байты. Точка держателя — `(x, f(x))`, x ∈ 1…255. Любые t
точек с разными x дают секрет (интерполяция Лагранжа в нуле); t − 1 точек о
секрете не говорят ничего: при неизвестных коэффициентах любое `s`
равновероятно. Прямая (`Line`, t = 2) — частный случай и прежний формат
точек: точки прямой поколения `M` и прямой аварийного ключа читаются и
`combine`, и `recover`.

Здесь только арифметика. Кто на какой точке сидит (второй сервер — 1, шлюзы —
по слотам, аварийный код — 255), пороги, поколения и ключи — `util/rsvkeys.py`.
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


def _pow(x: int, k: int) -> int:
    r = 1
    for _ in range(k):
        r = mul(r, x)
    return r


@dataclass(frozen=True)
class Poly:
    """Многочлен степени t − 1: `coeffs[0]` — секрет, остальные — случайные.
    Держит только тот, кто раздаёт точки."""
    coeffs: tuple[bytes, ...]

    def __post_init__(self) -> None:
        if len(self.coeffs) < 2:
            raise ValueError("порог многочлена — не меньше 2")
        n = len(self.coeffs[0])
        if not n or any(len(c) != n for c in self.coeffs):
            raise ValueError("коэффициенты — непустые строки байт одной длины")

    @classmethod
    def new(cls, secret: bytes, t: int) -> "Poly":
        """Новый многочлен под порог t (2…254) через секрет со случайными коэффициентами."""
        t = int(t)
        if not 2 <= t <= 254:
            raise ValueError("порог — от 2 до 254")
        if not secret:
            raise ValueError("секрет пуст")
        return cls((bytes(secret),) + tuple(secrets.token_bytes(len(secret)) for _ in range(t - 1)))

    @property
    def secret(self) -> bytes:
        return self.coeffs[0]

    @property
    def t(self) -> int:
        return len(self.coeffs)

    def point(self, x: int) -> Point:
        """Точка держателя x."""
        x = _check_x(x)
        out = bytearray(self.coeffs[0])
        for j in range(1, len(self.coeffs)):
            xj = _pow(x, j)
            for i, a in enumerate(self.coeffs[j]):
                out[i] ^= mul(a, xj)
        return x, bytes(out)

    def line(self) -> Line:
        """Прямая того же секрета и первого коэффициента (только при t = 2)."""
        if self.t != 2:
            raise ValueError("прямая — только у порога 2")
        return Line(self.coeffs[0], self.coeffs[1])


def lagrange_zero(points: list[Point]) -> bytes:
    """Значение многочлена в нуле по точкам с разными x: секрет, если точек не
    меньше порога (больше — тоже верно, если все с одного многочлена)."""
    pts = [(_check_x(x), bytes(y)) for x, y in points]
    if len(pts) < 2:
        raise ValueError("нужны хотя бы две точки")
    xs = [x for x, _ in pts]
    if len(set(xs)) != len(xs):
        raise ValueError("две точки с одним x секрет не задают")
    n = len(pts[0][1])
    if not n or any(len(y) != n for _, y in pts):
        raise ValueError("точки разной длины — это не один многочлен")
    out = bytearray(n)
    for i, (xi, yi) in enumerate(pts):
        num, den = 1, 1
        for j, (xj, _) in enumerate(pts):
            if j != i:
                num = mul(num, xj)
                den = mul(den, xi ^ xj)
        li = div(num, den)
        for k, y in enumerate(yi):
            out[k] ^= mul(y, li)
    return bytes(out)


def recover(*points) -> bytes:
    """Секрет по точкам: `recover(p1, p2)` (прямая, прежний вызов) или
    `recover([p1, p2, p3])` / `recover(p1, p2, p3)` — любые t точек многочлена."""
    if len(points) == 1 and not isinstance(points[0], tuple):
        points = tuple(points[0])
    if len(points) == 2:
        return combine(points[0], points[1]).secret
    return lagrange_zero(list(points))
