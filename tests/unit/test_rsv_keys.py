"""Ключи резерва серверов: Шамир «2 из n» над GF(2⁸) (util/shamir.py) и
главный ключ переезда `M` с поколениями, точками держателей и аварийным
кодом (util/rsvkeys.py).

Цена ошибки: арифметика поля, которая расходится с эталоном, даёт доли,
из которых `M` не собирается, — в аварию пакет резерва не откроется никем;
одна точка, говорящая что-то о секрете, — резерв в одиночку подбирает `M`;
аварийный код, который принимает опечатку, — «код не подошёл» без
подсказки, что именно не так; код прежнего поколения, который снимает
заголовок нового пакета, — утечка старого кода открывает новые данные.
Вывода ключа из пароля в этих модулях нет по построению (секрет случайный,
128 бит) — сторож по импортам держит это свойство.
"""
from __future__ import annotations

import ast
import itertools
import random
import re
from pathlib import Path

import pytest

from awgbot.util import rsvkeys, shamir
from awgbot.util.rsvpkg import seal

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _gf_mul_reference(a: int, b: int) -> int:
    """Эталон: умножение «в столбик» без переноса с приведением по 0x11B."""
    p = 0
    while b:
        if b & 1:
            p ^= a
        b >>= 1
        a <<= 1
        if a & 0x100:
            a ^= 0x11B
    return p


# ── арифметика поля ──────────────────────────────────────────────────────────

def test_the_field_multiplication_table_matches_the_reference_for_every_pair():
    """Все 65 536 произведений — против умножения в столбик: одна неверная
    клетка таблиц логарифмов ломает доли тех байт секрета, что на неё попали."""
    bad = [(a, b) for a in range(256) for b in range(256) if shamir.mul(a, b) != _gf_mul_reference(a, b)]
    assert not bad, f"умножение расходится с эталоном на {len(bad)} парах, например {bad[:3]}"


def test_division_undoes_multiplication_and_zero_is_not_a_divisor():
    for a in range(256):
        for b in range(1, 256):
            assert shamir.div(shamir.mul(a, b), b) == a, (a, b)
    with pytest.raises(ZeroDivisionError):
        shamir.div(7, 0)


# ── прямая: любые две точки, одна — ничего ───────────────────────────────────

@pytest.mark.parametrize("n", [2, 3, 4, 5, 6])
def test_any_two_points_out_of_n_give_back_the_secret(n):
    """Резерв, шлюзы по слотам и код — все пары держателей при n = 2…6:
    авария может застать любую пару живыми."""
    secret = bytes(range(16, 32))
    line = shamir.Line.new(secret)
    xs = [1, *range(2, n), 255][:n] if n > 2 else [1, 255]
    points = [line.point(x) for x in xs]
    for p1, p2 in itertools.permutations(points, 2):
        assert shamir.recover(p1, p2) == secret, f"точки x={p1[0]} и x={p2[0]} не дали секрет"
        assert shamir.combine(p1, p2) == line, "по двум точкам восстановлен не тот наклон"


def test_one_point_says_nothing_about_the_secret():
    """Для любой точки (x, y) и любого кандидата секрета s найдётся ровно один
    наклон, при котором прямая проходит через неё: все 256 значений байта
    секрета равновероятны. Иначе держатель одной доли (резерв) сужает перебор."""
    for x in range(1, 256):
        for y in (0, 1, 0x5A, 0xFF):
            candidates = sorted(y ^ shamir.mul(a, x) for a in range(256))
            assert candidates == list(range(256)), f"точка x={x}, y={y} исключает часть секретов"


def test_two_points_with_one_x_or_of_different_length_are_not_a_line():
    line = shamir.Line.new(b"\x01" * 16)
    with pytest.raises(ValueError):
        shamir.combine(line.point(2), line.point(2))
    with pytest.raises(ValueError):
        shamir.combine((1, b"\x00" * 16), (2, b"\x00" * 15))
    for x in (0, 256, -1):
        with pytest.raises(ValueError):
            line.point(x)
    with pytest.raises(ValueError):
        shamir.Line.new(b"")


# ── главный ключ M и его поколение ───────────────────────────────────────────

def test_m_is_16_random_bytes_and_the_box_key_is_32_bytes_and_not_m():
    m = rsvkeys.new_master()
    assert len(m) == 16 and rsvkeys.new_master() != m, "M не случайные 16 байт"
    k = rsvkeys.box_key(m)
    assert len(k) == 32, "ключ SecretBox — 32 байта"
    assert m not in k and k != m + m, "ключ SecretBox — не сам M и не его повтор"
    assert rsvkeys.box_key(m) == k, "растяжка M должна быть детерминированной — иначе заголовок не откроется"
    for bad in (b"", b"\x00" * 15, b"\x00" * 32):
        with pytest.raises(ValueError):
            rsvkeys.box_key(bad)


def test_points_of_a_generation_sit_where_the_holders_expect_them():
    g = rsvkeys.Generation.new(1)
    assert g.reserve_point()[0] == 1, "резерв — x = 1"
    assert [g.gateway_point(s)[0] for s in (1, 2, 3)] == [2, 3, 4], "шлюз слота N — x = N + 1"
    assert rsvkeys.code_point(g.code())[0] == 255, "аварийный код — x = 255"
    for bad in (0, 254):
        with pytest.raises(ValueError):
            g.gateway_point(bad)


def test_the_code_with_either_share_gives_m_and_alone_it_does_not():
    g = rsvkeys.Generation.new(3)
    code = rsvkeys.code_point(g.code())
    assert rsvkeys.recover_m(g.reserve_point(), g.gateway_point(1)) == g.m, "резерв + шлюз"
    assert rsvkeys.recover_m(g.reserve_point(), code) == g.m, "резерв + код (авария Н4)"
    assert rsvkeys.recover_m(g.gateway_point(2), code) == g.m, "шлюз + код"
    assert code[1] != g.m, "сам код — не M"


def test_a_new_generation_changes_m_the_slope_every_point_and_the_code():
    a, b = rsvkeys.Generation.new(1), rsvkeys.Generation.new(2)
    assert a.m != b.m and a.line.slope != b.line.slope
    assert a.reserve_point() != b.reserve_point() and a.gateway_point(1) != b.gateway_point(1)
    assert a.code() != b.code()


def test_the_old_code_with_the_new_reserve_share_does_not_open_the_new_header():
    """Код прежнего поколения + доля резерва нового — не тот M: заголовок
    пакета нового поколения не снимается (а не «снимается мусором»)."""
    old, new = rsvkeys.Generation.new(1), rsvkeys.Generation.new(2)
    k = seal.new_k()
    blob = seal.seal({"vpn": {"x": 1}}, k=k, m=new.m, pkg_gen=1, m_gen=2)
    wrong = rsvkeys.recover_m(new.reserve_point(), rsvkeys.code_point(old.code()))
    assert wrong != new.m
    with pytest.raises(seal.PackageError):
        seal.open_k(blob, wrong)
    assert seal.open_k(blob, rsvkeys.recover_m(new.reserve_point(), rsvkeys.code_point(new.code()))) == k


def test_shares_of_two_generations_do_not_open_either_package():
    """Доли разных поколений не складываются: доля шлюза прежнего поколения с
    долей резерва нового — ни один из двух заголовков не снимается."""
    old, new = rsvkeys.Generation.new(1), rsvkeys.Generation.new(2)
    mixed = rsvkeys.recover_m(new.reserve_point(), old.gateway_point(1))
    for g in (old, new):
        blob = seal.seal({"vpn": {}}, k=seal.new_k(), m=g.m, pkg_gen=1, m_gen=g.gen)
        with pytest.raises(seal.PackageError):
            seal.open_k(blob, mixed)


# ── аварийный код строкой ────────────────────────────────────────────────────

_CODE_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{4}(-[0-9A-HJKMNP-TV-Z]{4}){6}\Z")


def test_the_code_is_seven_groups_of_four_crockford_symbols_and_reads_back():
    for _ in range(50):
        y = rsvkeys.new_master()
        text = rsvkeys.code_encode(y)
        assert _CODE_RE.match(text), text
        assert rsvkeys.code_decode(text) == y


def test_the_code_input_forgives_case_spaces_dashes_and_lookalike_letters():
    y = bytes(range(16))
    text = rsvkeys.code_encode(y)
    variants = [text.lower(), text.replace("-", " "), text.replace("-", ""), " " + text.replace("-", ".") + "\n",
                text.replace("-", "_"),
                text.replace("0", "O").replace("1", "I"), text.replace("1", "l")]
    for v in variants:
        assert rsvkeys.code_decode(v) == y, f"ввод {v!r} не прочитан"


def _single_symbol_typos(text: str):
    flat = text.replace("-", "")
    for i, ch in enumerate(flat):
        for sub in rsvkeys.ALPHABET:
            if sub != ch:
                yield flat[:i] + sub + flat[i + 1:]


def test_a_single_wrong_symbol_is_caught_by_the_checksum():
    """Опечатка в одном знаке из 28 — «код не сошёлся», а не тихо другая
    точка: с другой точкой процесс открытия соберёт не тот M и скажет «код не
    подошёл», и админ будет искать ошибку не там. Проверяются все замены
    одного знака у двадцати кодов с фиксированным зерном."""
    rnd = random.Random(20261020)
    missed = []
    for _ in range(20):
        y = bytes(rnd.randrange(256) for _ in range(16))
        text = rsvkeys.code_encode(y)
        for typo in _single_symbol_typos(text):
            try:
                rsvkeys.code_decode(typo)
            except ValueError:
                continue
            missed.append((text, typo))
    assert not missed, f"контрольная сумма пропустила {len(missed)} опечаток в один знак, например {missed[:2]}"


@pytest.mark.parametrize("text", ["", "ABCD-EFGH", "ABCD-EFGH-JKMN-PQRS-TVWX-YZ23-456", "ABCD-EFGH-JKMN-PQRS-TVWX-YZ23-45U6",
                                  "привет"])
def test_a_code_of_the_wrong_shape_is_refused(text):
    assert rsvkeys.normalize(text) is None
    with pytest.raises(ValueError):
        rsvkeys.code_decode(text)


# ── сторож: никакого вывода ключа из пароля ──────────────────────────────────

_PASSWORD_KDF = ("pwhash", "argon2", "scrypt", "pbkdf2", "bcrypt")


@pytest.mark.parametrize("rel", ["awgbot/util/rsvkeys.py", "awgbot/util/shamir.py"])
def test_the_key_modules_never_derive_a_key_from_a_password(rel):
    """M — случайные 128 бит, код — точка прямой: вывод из пароля здесь лишний
    и дорогой (секунды на процесс открытия в аварию). Ни импорта
    `nacl.pwhash`/argon2, ни вызова scrypt/pbkdf2."""
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names += [node.module or ""] + [a.name for a in node.names]
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.Name):
            names.append(node.id)
    hits = sorted({n for n in names if any(w in n.lower() for w in _PASSWORD_KDF)})
    assert not hits, f"{rel}: вывод ключа из пароля — {hits}"
