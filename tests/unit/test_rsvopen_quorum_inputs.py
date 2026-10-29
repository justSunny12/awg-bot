"""Вход процесса открытия под кворумом (tools/rsvopen.recover_keys; концепт
«кворум» §2.7, §4.9): `qpoints` + `qbox` — t точек многочлена ключа кворума и
ящик кворума; `code` + `cpoint` + `cbox` — аварийный код, точка резерва прямой
аварийного ключа и ящик кода; прежние `shares` и `share` + `code` — для линии на
матрице. Пакет — настоящий (util/rsvpkg/seal), ключи — util/rsvkeys.

Цена ошибки: вход, открывающий пакет меньшим числом точек, чем порог, — одна
сторона (резерв и один шлюз) увозит пользователей мимо большинства; код прямой
`M`, принятый ящиком кода, — прежний код после включения голосования всё ещё
действует; падение с трассой вместо OpenError — бот резерва показывает
человеку обрыв процесса вместо причины, а попытка кода не засчитана.
"""
from __future__ import annotations

import pytest

from awgbot.util import rsvkeys
from awgbot.util.rsvpkg import seal
from tools import rsvopen

pytestmark = pytest.mark.unit


class _World:
    """Основа одного поколения: M, K, пакет, многочлен порога t и прямая кода."""

    def __init__(self, t: int = 2) -> None:
        self.gen = rsvkeys.Generation.new(1)
        self.m = self.gen.m
        self.k = seal.new_k()
        self.blob = seal.seal({"vpn": {"peers": []}}, k=self.k, m=self.m, pkg_gen=4, m_gen=1)
        self.q = rsvkeys.QuorumGen.new(1, t)
        self.c = rsvkeys.CodeGen.new(1)
        self.qbox = self.q.qbox(self.m).hex()
        self.cbox = self.c.cbox(self.m).hex()

    def qpoint(self, x: int) -> list:
        px, py = self.q.poly.point(x)
        return [px, py.hex()]

    def cpoint(self) -> list:
        x, y = self.c.reserve_point()
        return [x, y.hex()]


@pytest.mark.parametrize("t", [2, 3, 4, 5])
def test_t_points_of_the_majority_polynomial_and_the_quorum_box_give_m_and_k(t):
    w = _World(t)
    pts = [w.qpoint(1)] + [w.qpoint(rsvkeys.gateway_x(s)) for s in range(1, t)]
    assert rsvopen.recover_keys({"qpoints": pts, "qbox": w.qbox}, w.blob) == (w.m, w.k)


@pytest.mark.parametrize("t", [3, 4, 5])
def test_fewer_points_than_the_threshold_are_a_clean_failure(t):
    w = _World(t)
    pts = [w.qpoint(1)] + [w.qpoint(rsvkeys.gateway_x(s)) for s in range(1, t - 1)]
    with pytest.raises(rsvopen.OpenError):
        rsvopen.recover_keys({"qpoints": pts, "qbox": w.qbox}, w.blob)


@pytest.mark.parametrize("pts", [[[1, "ab" * 16]], [[2, "ab" * 16], [2, "cd" * 16]]], ids=["one_point", "same_x"])
def test_one_point_or_a_repeated_point_is_refused_before_any_math(pts):
    w = _World()
    with pytest.raises(rsvopen.OpenError, match="мало или они одной точки"):
        rsvopen.recover_keys({"qpoints": pts, "qbox": w.qbox}, w.blob)


@pytest.mark.parametrize("pts", [[[0, "ab" * 16], [2, "cd" * 16]], [[1, "zz"], [2, "cd" * 16]], [[1], [2, "cd" * 16]],
                                 [[1, "ab" * 15], [2, "cd" * 16]]], ids=["x_zero", "not_hex", "no_y", "short_y"])
def test_a_garbled_point_is_an_open_error_not_a_trace(pts):
    w = _World()
    with pytest.raises(rsvopen.OpenError):
        rsvopen.recover_keys({"qpoints": pts, "qbox": w.qbox}, w.blob)


def test_points_of_another_polynomial_do_not_open_the_quorum_box():
    w, other = _World(), _World()
    with pytest.raises(rsvopen.OpenError, match="ключ кворума не собрался"):
        rsvopen.recover_keys({"qpoints": [other.qpoint(1), other.qpoint(2)], "qbox": w.qbox}, w.blob)


def test_a_quorum_box_of_another_m_does_not_open_the_package():
    """Ящик прежнего поколения M (резерв не получил новые qkeys): M собирается, но не то."""
    w, old = _World(), _World()
    old_box = w.q.qbox(old.m).hex()
    with pytest.raises(rsvopen.OpenError, match="ключ пакета"):
        rsvopen.recover_keys({"qpoints": [w.qpoint(1), w.qpoint(2)], "qbox": old_box}, w.blob)


def test_the_code_with_the_reserve_code_point_and_the_code_box_give_m_and_k():
    w = _World()
    for typed in (w.c.code(), w.c.code().lower().replace("-", " ")):
        assert rsvopen.recover_keys({"code": typed, "cpoint": w.cpoint(), "cbox": w.cbox}, w.blob) == (w.m, w.k)


def test_the_code_of_the_line_of_m_does_not_open_the_code_box():
    """Прежний код (точка x = 255 прямой самого M) после включения — отказ."""
    w = _World()
    with pytest.raises(rsvopen.OpenError, match="аварийный код"):
        rsvopen.recover_keys({"code": w.gen.code(), "cpoint": w.cpoint(), "cbox": w.cbox}, w.blob)


def test_a_mistyped_code_under_the_quorum_is_refused_with_the_code_reason():
    w = _World()
    code = w.c.code()
    bad = code[:-1] + ("0" if code[-1] != "0" else "1")
    with pytest.raises(rsvopen.OpenError, match="аварийный код"):
        rsvopen.recover_keys({"code": bad, "cpoint": w.cpoint(), "cbox": w.cbox}, w.blob)


def test_the_matrix_inputs_still_open_as_before():
    """Линия на матрице: две доли прямой M или доля резерва + код прямой M."""
    w = _World()
    r = [1, w.gen.reserve_point()[1].hex()]
    g = [2, w.gen.gateway_point(1)[1].hex()]
    assert rsvopen.recover_keys({"shares": [r, g]}, w.blob) == (w.m, w.k)
    assert rsvopen.recover_keys({"share": r, "code": w.gen.code()}, w.blob) == (w.m, w.k)


def test_a_quorum_code_with_the_old_share_input_does_not_open():
    """Код прямой Mc в прежнем входе (share + code) — не точка прямой M: отказ."""
    w = _World()
    with pytest.raises(rsvopen.OpenError):
        rsvopen.recover_keys({"share": [1, w.gen.reserve_point()[1].hex()], "code": w.c.code()}, w.blob)


def test_a_partial_quorum_input_is_not_taken_for_a_full_one():
    """Код без ящика кода (точка есть) — не вход кода под кворумом и не прежний вход."""
    w = _World()
    with pytest.raises(rsvopen.OpenError):
        rsvopen.recover_keys({"code": w.c.code(), "cpoint": w.cpoint()}, w.blob)
    with pytest.raises(rsvopen.OpenError):
        rsvopen.recover_keys({"qpoints": [w.qpoint(1), w.qpoint(2)]}, w.blob)
