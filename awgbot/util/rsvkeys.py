"""
rsvkeys.py — главный ключ переезда `M`, его поколения и точки держателей; обе роли.

`M` — 16 случайных байт поколения. Ключ SecretBox для заголовка пакета и
конверта DNS — растяжка `BLAKE2b(M)` до 32 байт: не вывод из пароля, а
приведение случайных 128 бит к длине ключа; перебор — 2¹²⁸.

Точки одной прямой Шамира «2 из n» (`util/shamir.py`): резерв — x = 1, шлюзы —
по слотам (слот 1 → x = 2, …), **аварийный код админа — x = 255**: код —
это точка, записанная base32 с контрольной суммой, 7 групп по 4 знака
(`ABCD-EFGH-JKLM-NPQR-STUV-WXYZ-2345`). Код + доля резерва дают `M`, как и
доля шлюза + доля резерва; одна точка не даёт ничего.

На одной прямой любые две точки раскрывают все: поколение (и с ним код)
меняется, когда прямая могла стать известна резерву — переезд по доле
шлюза, по коду, снятие или замена шлюза, смена кода по желанию. Новое
поколение — новые `M`, наклон и все точки.

Ввод кода прощает руки: регистр, пробелы, дефисы, точки, подчёркивания не
важны, `O` → `0`, `I`/`L` → `1`; контрольная сумма ловит порчу знака.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from . import shamir

M_LEN = 16
RESERVE_X = 1
CODE_X = 255
_PERSON = b"awgbot-rsv-m-v1"

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"      # Crockford base32, как у кода сопряжения
_CODE_GROUPS = 7
_CODE_GROUP = 4
_CODE_CHARS = _CODE_GROUPS * _CODE_GROUP            # 28 знаков × 5 бит = 140 бит
_SUM_BITS = _CODE_CHARS * 5 - M_LEN * 8             # 12 бит контрольной суммы
_DROP = str.maketrans("", "", " \t\n-._")
_FIX = str.maketrans({"O": "0", "I": "1", "L": "1"})


def new_master() -> bytes:
    return secrets.token_bytes(M_LEN)


def box_key(m: bytes) -> bytes:
    """32-байтный ключ SecretBox из `M`."""
    if len(m) != M_LEN:
        raise ValueError(f"M — {M_LEN} байт")
    return hashlib.blake2b(bytes(m), digest_size=32, person=_PERSON).digest()


def gateway_x(slot: int) -> int:
    """Точка шлюза по номеру слота: слот 1 → 2, слот 2 → 3, …"""
    slot = int(slot)
    if not 1 <= slot <= 253:
        raise ValueError("номер слота для точки — от 1 до 253")
    return slot + 1


@dataclass(frozen=True)
class Generation:
    """Поколение `M`: номер, ключ и наклон прямой. Живёт только у основы."""
    gen: int
    line: shamir.Line

    @classmethod
    def new(cls, gen: int) -> "Generation":
        return cls(int(gen), shamir.Line.new(new_master()))

    @property
    def m(self) -> bytes:
        return self.line.secret

    def reserve_point(self) -> shamir.Point:
        return self.line.point(RESERVE_X)

    def gateway_point(self, slot: int) -> shamir.Point:
        return self.line.point(gateway_x(slot))

    def code(self) -> str:
        """Аварийный код — точка x = 255, строкой для человека."""
        return code_encode(self.line.point(CODE_X)[1])


def recover_m(p1: shamir.Point, p2: shamir.Point) -> bytes:
    """`M` по двум точкам (резерв + шлюз, резерв + код)."""
    m = shamir.recover(p1, p2)
    if len(m) != M_LEN:
        raise ValueError("точки не той длины")
    return m


def code_point(code: str) -> shamir.Point:
    """Точка аварийного кода из строки человека; порча — ValueError."""
    return CODE_X, code_decode(code)


# ── аварийный код: base32 с контрольной суммой ───────────────────────────────

_CRC_POLY = 0x80F          # CRC-12: любой всплеск ошибок до 12 бит подряд ловится — опечатка в одном знаке
                           # base32 меняет не больше 5 бит подряд


def _sum(y: bytes) -> int:
    """CRC-12 над битами точки (старшим битом вперёд), а не хеш: хеш пропускал
    бы примерно одну из 4096 опечаток, CRC — ни одной одиночной."""
    n = int.from_bytes(y, "big")
    crc = 0
    for i in range(len(y) * 8 - 1, -1, -1):
        bit = (n >> i) & 1
        top = (crc >> (_SUM_BITS - 1)) & 1
        crc = (crc << 1) & ((1 << _SUM_BITS) - 1)
        if top ^ bit:
            crc ^= _CRC_POLY
    return crc


def code_encode(y: bytes) -> str:
    """16 байт точки → `ABCD-EFGH-…` (7 групп по 4 знака)."""
    if len(y) != M_LEN:
        raise ValueError(f"точка кода — {M_LEN} байт")
    n = (int.from_bytes(y, "big") << _SUM_BITS) | _sum(y)
    chars = []
    for _ in range(_CODE_CHARS):
        chars.append(ALPHABET[n & 31])
        n >>= 5
    s = "".join(reversed(chars))
    return "-".join(s[i:i + _CODE_GROUP] for i in range(0, _CODE_CHARS, _CODE_GROUP))


def normalize(text: str) -> str | None:
    """Ввод человека → 28 знаков алфавита или None (не тот формат)."""
    s = str(text or "").upper().translate(_DROP).translate(_FIX)
    if len(s) != _CODE_CHARS or any(ch not in ALPHABET for ch in s):
        return None
    return s


def code_decode(text: str) -> bytes:
    """Строка кода → 16 байт точки; не тот формат или не сошлась сумма — ValueError."""
    s = normalize(text)
    if s is None:
        raise ValueError("аварийный код — 7 групп по 4 знака")
    n = 0
    for ch in s:
        n = (n << 5) | ALPHABET.index(ch)
    got, body = n & ((1 << _SUM_BITS) - 1), n >> _SUM_BITS
    y = body.to_bytes(M_LEN, "big")
    if got != _sum(y):
        raise ValueError("аварийный код не сошёлся — проверь знаки")
    return y
