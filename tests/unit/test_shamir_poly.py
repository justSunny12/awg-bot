"""Шамир общего порога (util/shamir.py: Poly, lagrange_zero, recover) — ключ
кворума резерва `Mq` раздаётся многочленом степени t − 1 (концепт «кворум» §2.7).

Цена ошибки: многочлен, по которому t точек не собирают секрет, — в аварию
большинство голосующих не откроет ящик кворума, пакет переезда не откроется
никем; t − 1 точек, говорящих что-то о секрете, — меньшинство (сторона резерва
со своей одной точкой и одним шлюзом) сужает перебор и подбирает `Mq` мимо
большинства. Прямая — частный случай t = 2: прежние вызовы `recover(p1, p2)`
и `combine` на точках прежних поколений должны давать то же, что и раньше,
иначе пакет, запечатанный до кворума, перестанет открываться.
"""
from __future__ import annotations

import itertools
import random

import pytest

from awgbot.util import shamir

pytestmark = pytest.mark.unit

THRESHOLDS = range(2, 9)


def _xs(t: int, extra: int = 2) -> list[int]:
    """Держатели как в линии: второй сервер x = 1, шлюзы x = слот + 1, и ещё пара сверх порога."""
    return list(range(1, t + 1 + extra))


# ── любые t точек дают секрет ────────────────────────────────────────────────

@pytest.mark.parametrize("t", THRESHOLDS)
def test_any_t_points_give_back_the_secret_and_more_than_t_too(t):
    """Авария застаёт живыми любые t держателей: каждое сочетание из t точек
    (и любое большее — лишние точки с того же многочлена не мешают) даёт секрет."""
    secret = bytes(range(100, 116))
    poly = shamir.Poly.new(secret, t)
    assert poly.t == t and poly.secret == secret
    points = [poly.point(x) for x in _xs(t)]
    for k in range(t, len(points) + 1):
        for combo in itertools.combinations(points, k):
            got = shamir.recover(list(combo))
            assert got == secret, f"t={t}: {k} точек x={[p[0] for p in combo]} не дали секрет"


@pytest.mark.parametrize("t", THRESHOLDS)
def test_recover_takes_points_positionally_as_well_as_a_list(t):
    """`recover(p1, p2, p3)` и `recover([p1, p2, p3])` — одно и то же: процесс
    открытия передаёт точки и так, и так."""
    secret = b"\x42" * 16
    poly = shamir.Poly.new(secret, t)
    pts = [poly.point(x) for x in (255, *range(2, t + 1))]
    assert shamir.recover(*pts) == secret
    assert shamir.recover(pts) == secret
    assert shamir.lagrange_zero(pts) == secret


@pytest.mark.parametrize("t", THRESHOLDS)
def test_the_order_of_points_does_not_matter(t):
    poly = shamir.Poly.new(bytes(range(16)), t)
    pts = [poly.point(x) for x in _xs(t, 0)]
    rnd = random.Random(t)
    for _ in range(5):
        rnd.shuffle(pts)
        assert shamir.recover(pts) == poly.secret, f"t={t}: порядок {[p[0] for p in pts]} сломал восстановление"


# ── t − 1 точек — ни одного бита ─────────────────────────────────────────────

@pytest.mark.parametrize("t", THRESHOLDS)
def test_t_minus_one_points_leave_every_byte_of_the_secret_equally_possible(t):
    """При t − 1 известных точках каждый вариант недостающей t-й точки даёт свой
    «секрет», и все 256 значений байта встречаются ровно по разу: меньшинство не
    отличит настоящий секрет ни от какого другого."""
    rnd = random.Random(1000 + t)
    for _ in range(8):
        secret = bytes([rnd.randrange(256)])
        poly = shamir.Poly.new(secret, t)
        known = [poly.point(x) for x in rnd.sample(range(1, 256), t)]
        missing_x, known = known[0][0], known[1:]
        freq = [0] * 256
        for y in range(256):
            freq[shamir.lagrange_zero(known + [(missing_x, bytes([y]))])[0]] += 1
        assert freq == [1] * 256, f"t={t}: при переборе недостающей точки секреты неравновероятны"


def _chi2(counts: list[int]) -> float:
    n = sum(counts)
    e = n / len(counts)
    return sum((c - e) ** 2 / e for c in counts)


# хи-квадрат при 255 степенях свободы: среднее 255, σ ≈ 22,6; 400 — дальше шести σ
_CHI2_LIMIT = 400


@pytest.mark.parametrize("t", THRESHOLDS)
def test_t_minus_one_points_look_the_same_whatever_the_secret(t):
    """Статистика на 10⁴ прогонов: значения t − 1 точек (их сумма в поле и каждая
    по отдельности) распределены равномерно и одинаково при секрете 0x00 и 0xFF —
    по точкам меньшинства секрет не угадывается и частотами."""
    runs = 10_000
    for secret in (b"\x00", b"\xff"):
        xs = list(range(1, t))                    # ровно t − 1 держателей
        per_x = {x: [0] * 256 for x in xs}
        mixed = [0] * 256
        for _ in range(runs):
            poly = shamir.Poly.new(secret, t)
            acc = 0
            for x in xs:
                y = poly.point(x)[1][0]
                per_x[x][y] += 1
                acc ^= y
            mixed[acc] += 1
        for x, counts in per_x.items():
            assert _chi2(counts) < _CHI2_LIMIT, f"t={t}, секрет {secret.hex()}: точка x={x} неравномерна"
        assert _chi2(mixed) < _CHI2_LIMIT, f"t={t}, секрет {secret.hex()}: сумма точек выдаёт секрет"


def test_the_statistics_check_catches_a_leaking_share():
    """Сам критерий не слепой: «точка», в которой секрет просвечивает (младший бит
    равен биту секрета), даёт хи-квадрат далеко за порогом."""
    rnd = random.Random(7)
    counts = [0] * 256
    for _ in range(10_000):
        counts[(rnd.randrange(256) & 0xFE) | 1] += 1
    assert _chi2(counts) > _CHI2_LIMIT


# ── прямая — частный случай ───────────────────────────────────────────────────

def test_a_threshold_two_polynomial_is_exactly_the_old_line():
    """`Poly.new(s, 2)` и `Line` той же пары (секрет, наклон) — одна геометрия: все
    255 точек совпадают, `combine` по точкам многочлена отдаёт ту же прямую."""
    poly = shamir.Poly.new(bytes(range(16)), 2)
    line = poly.line()
    same = shamir.Line(poly.coeffs[0], poly.coeffs[1])
    assert line == same
    for x in range(1, 256):
        assert poly.point(x) == line.point(x), f"точка x={x} многочлена и прямой разошлась"
    assert shamir.combine(poly.point(1), poly.point(255)) == line


def test_old_points_of_a_line_open_with_the_old_and_the_new_call():
    """Точки прямой прежнего поколения (резерв x = 1, шлюз x = 2, код x = 255):
    `recover(p1, p2)` прежним вызовом, `recover([p1, p2])` и `lagrange_zero` —
    один и тот же секрет."""
    secret = bytes(range(16, 32))
    line = shamir.Line.new(secret)
    for p1, p2 in itertools.permutations([line.point(1), line.point(2), line.point(255)], 2):
        assert shamir.recover(p1, p2) == secret
        assert shamir.recover([p1, p2]) == secret
        assert shamir.lagrange_zero([p1, p2]) == secret


def test_known_line_values_are_stable():
    """Значения прямой с заданными секретом и наклоном зафиксированы: смена поля
    или формулы точки тихо сломала бы точки, уже розданные держателям."""
    line = shamir.Line(b"\x00\x01\x80\xff", b"\x03\x03\x03\x03")
    assert line.point(1) == (1, bytes([0x03, 0x02, 0x83, 0xFC]))
    # 3·2 = 6, 3·255 в GF(2⁸) с 0x11B = 0x1A
    assert line.point(2) == (2, bytes([0x06, 0x07, 0x86, 0xF9]))
    assert line.point(255)[1] == bytes([0x1A, 0x1B, 0x9A, 0xE5])
    poly = shamir.Poly((line.secret, line.slope))
    for x in (1, 2, 255):
        assert poly.point(x) == line.point(x)


def test_line_is_refused_for_a_threshold_above_two():
    with pytest.raises(ValueError):
        shamir.Poly.new(b"\x01", 3).line()


# ── ошибки ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("t", [0, 1, 255, 256, -2])
def test_a_threshold_outside_two_to_254_is_refused(t):
    with pytest.raises(ValueError):
        shamir.Poly.new(b"\x01" * 16, t)


def test_the_edge_thresholds_two_and_254_are_accepted():
    for t in (2, 254):
        assert shamir.Poly.new(b"\x01", t).t == t


def test_an_empty_secret_or_ragged_coefficients_are_refused():
    with pytest.raises(ValueError):
        shamir.Poly.new(b"", 3)
    with pytest.raises(ValueError):
        shamir.Poly((b"\x01\x02", b"\x03"))
    with pytest.raises(ValueError):
        shamir.Poly((b"\x01",))


def test_points_with_one_x_or_of_different_length_are_refused():
    """Две точки с одним x (держатель прислал свою точку дважды) и точки разной
    длины — ValueError, а не «секрет», собранный из мусора."""
    poly = shamir.Poly.new(b"\x07" * 16, 3)
    p1, p2, p3 = poly.point(1), poly.point(2), poly.point(3)
    with pytest.raises(ValueError):
        shamir.recover([p1, p2, p2])
    with pytest.raises(ValueError):
        shamir.recover(p1, p1)
    with pytest.raises(ValueError):
        shamir.recover([p1, p2, (3, p3[1][:15])])
    with pytest.raises(ValueError):
        shamir.recover(p1, (2, p2[1][:15]))
    with pytest.raises(ValueError):
        shamir.lagrange_zero([p1])
    for x in (0, 256):
        with pytest.raises(ValueError):
            poly.point(x)
        with pytest.raises(ValueError):
            shamir.recover([p1, p2, (x, p3[1])])
