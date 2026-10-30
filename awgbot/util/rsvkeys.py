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

**Кворум (4.2.0) — два ящика вокруг `M`** вместо точек самого `M`:

- ключ кворума `Mq` (16 байт) — многочлен порога t (большинство голосующих
  линии, `util/shamir.Poly`): второй сервер x = 1, шлюзы основы x = слот + 1;
  ящик кворума `qbox = SecretBox(ключ из Mq)(M)`;
- аварийный ключ `Mc` (16 байт) — прямая: второй сервер x = 1, код x = 255
  (тот же формат кода); ящик кода `cbox = SecretBox(ключ из Mc)(M)`.

Открытие: t точек → `Mq` → `qbox` → `M`; точка резерва прямой кода + код →
`Mc` → `cbox` → `M`. Смена состава без снятия держателя меняет только `Mq`,
точки и `qbox`; снятие или замена — новое поколение `M` и оба ящика; код и
прямая `Mc` меняются только после переезда по коду и по «🔄 Сменить код».
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from nacl.exceptions import CryptoError
from nacl.secret import SecretBox

from . import shamir

M_LEN = 16
RESERVE_X = 1
CODE_X = 255
_PERSON = b"awgbot-rsv-m-v1"
_PERSON_Q = b"awgbot-rsv-mq-v1"
_PERSON_C = b"awgbot-rsv-mc-v1"
BOX_LEN = 24 + M_LEN + 16                            # нонс + M + MAC

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
        raise ValueError(f"главный ключ — {M_LEN} байт")
    return hashlib.blake2b(bytes(m), digest_size=32, person=_PERSON).digest()


def gateway_x(slot: int) -> int:
    """Точка шлюза по номеру слота: слот 1 → 2, слот 2 → 3, …"""
    slot = int(slot)
    if not 1 <= slot <= 253:
        raise ValueError("номер слота для точки — от 1 до 253")
    return slot + 1


def code_point(code: str) -> shamir.Point:
    """Точка аварийного кода из строки человека (x = 255 прямой `Mc`); порча — ValueError."""
    return CODE_X, code_decode(code)


# ── кворум: Mq, Mc и ящики ───────────────────────────────────────────────────

def _key(m: bytes, person: bytes) -> bytes:
    if len(m) != M_LEN:
        raise ValueError(f"ключ ящика — {M_LEN} байт")
    return hashlib.blake2b(bytes(m), digest_size=32, person=person).digest()


def qbox_key(mq: bytes) -> bytes:
    """32-байтный ключ ящика кворума из `Mq`."""
    return _key(mq, _PERSON_Q)


def cbox_key(mc: bytes) -> bytes:
    """32-байтный ключ ящика кода из `Mc`."""
    return _key(mc, _PERSON_C)


def seal_box(key: bytes, m: bytes) -> bytes:
    """Ящик: `M` под ключом ящика (SecretBox, случайный нонс)."""
    if len(m) != M_LEN:
        raise ValueError(f"главный ключ — {M_LEN} байт")
    return bytes(SecretBox(bytes(key)).encrypt(bytes(m)))


def open_box(key: bytes, box: bytes) -> bytes:
    """`M` из ящика; чужой ключ или порча — ValueError."""
    try:
        m = bytes(SecretBox(bytes(key)).decrypt(bytes(box)))
    except (CryptoError, ValueError) as e:
        raise ValueError("ящик не открылся: не тот ключ или порча") from e
    if len(m) != M_LEN:
        raise ValueError("в ящике не главный ключ")
    return m


@dataclass(frozen=True)
class QuorumGen:
    """Поколение ключа кворума: номер, порог и многочлен `Mq`. Живёт только у основы."""
    q_gen: int
    poly: shamir.Poly

    @classmethod
    def new(cls, q_gen: int, t: int) -> "QuorumGen":
        return cls(int(q_gen), shamir.Poly.new(secrets.token_bytes(M_LEN), t))

    @property
    def mq(self) -> bytes:
        return self.poly.secret

    @property
    def t(self) -> int:
        return self.poly.t

    def reserve_point(self) -> shamir.Point:
        return self.poly.point(RESERVE_X)

    def gateway_point(self, slot: int) -> shamir.Point:
        return self.poly.point(gateway_x(slot))

    def qbox(self, m: bytes) -> bytes:
        return seal_box(qbox_key(self.mq), m)


@dataclass(frozen=True)
class CodeGen:
    """Поколение аварийного ключа: номер и прямая `Mc` (второй сервер — 1, код — 255)."""
    c_gen: int
    line: shamir.Line

    @classmethod
    def new(cls, c_gen: int) -> "CodeGen":
        return cls(int(c_gen), shamir.Line.new(secrets.token_bytes(M_LEN)))

    @property
    def mc(self) -> bytes:
        return self.line.secret

    def reserve_point(self) -> shamir.Point:
        return self.line.point(RESERVE_X)

    def code(self) -> str:
        return code_encode(self.line.point(CODE_X)[1])

    def cbox(self, m: bytes) -> bytes:
        return seal_box(cbox_key(self.mc), m)


def recover_m_quorum(points: list[shamir.Point], qbox: bytes) -> bytes:
    """`M` по t точкам многочлена `Mq` и ящику кворума; мало точек или чужие —
    ValueError (ящик не откроется)."""
    pts = [(int(x), bytes(y)) for x, y in points]
    if any(len(y) != M_LEN for _, y in pts):
        raise ValueError("точки не той длины")
    mq = shamir.recover(pts)
    return open_box(qbox_key(mq), qbox)


def recover_m_code(code: str, cpoint: shamir.Point, cbox: bytes) -> bytes:
    """`M` по аварийному коду, точке резерва прямой `Mc` и ящику кода."""
    p = (int(cpoint[0]), bytes(cpoint[1]))
    if len(p[1]) != M_LEN:
        raise ValueError("точка не той длины")
    mc = shamir.recover(p, code_point(code))
    return open_box(cbox_key(mc), cbox)


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
