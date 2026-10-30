"""Вход процесса открытия (tools/rsvopen.recover_keys): `qpoints` + `qbox` — t
точек многочлена ключа кворума и ящик кворума; `code` + `cpoint` + `cbox` —
аварийный код, точка резерва прямой аварийного ключа и ящик кода. Прямой «2 из
n» самого `M` нет: прежние входы `shares` и `share` + `code` не открывают ничего.
Пакет — настоящий (util/rsvpkg/seal), ключи — util/rsvkeys.

Цена ошибки: вход, открывающий пакет меньшим числом точек, чем порог, — одна
сторона (резерв и один шлюз) увозит пользователей мимо большинства; код другой
прямой, принятый ящиком кода, — прежний код после смены всё ещё действует;
прежний вход долей, принятый процессом, — путь, которого у основы больше нет,
и ошибка в нём не ловится ничем; падение с трассой вместо OpenError — бот
резерва показывает человеку обрыв процесса вместо причины, а попытка кода не
засчитана.
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
        self.m = rsvkeys.new_master()
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


def test_a_code_of_another_generation_does_not_open_the_code_box():
    """Код прежней прямой `Mc` (до «🔄 Сменить код» или переезда по коду) с точкой и
    ящиком нового поколения — отказ: старый код после смены не действует."""
    w = _World()
    old = rsvkeys.CodeGen.new(1)
    with pytest.raises(rsvopen.OpenError, match="аварийный код"):
        rsvopen.recover_keys({"code": old.code(), "cpoint": w.cpoint(), "cbox": w.cbox}, w.blob)


def test_a_code_box_of_another_line_is_refused_even_with_the_right_code_and_point():
    """Ящик кода чужой прямой (другая основа или прежнее поколение `Mc`) — отказ, а не
    мусорный `M`: ящик не открывается ключом из этой прямой."""
    w, other = _World(), _World()
    with pytest.raises(rsvopen.OpenError, match="аварийный код"):
        rsvopen.recover_keys({"code": w.c.code(), "cpoint": w.cpoint(), "cbox": other.cbox}, w.blob)


def test_a_code_box_of_another_m_opens_the_box_but_not_the_package():
    """Ящик кода прежнего поколения `M` (резерв не получил новые qkeys): `M` собирается,
    но пакет нового поколения им не открыть."""
    w, old = _World(), _World()
    stale = w.c.cbox(old.m).hex()
    with pytest.raises(rsvopen.OpenError, match="ключ пакета"):
        rsvopen.recover_keys({"code": w.c.code(), "cpoint": w.cpoint(), "cbox": stale}, w.blob)


def test_a_mistyped_code_under_the_quorum_is_refused_with_the_code_reason():
    w = _World()
    code = w.c.code()
    bad = code[:-1] + ("0" if code[-1] != "0" else "1")
    with pytest.raises(rsvopen.OpenError, match="аварийный код"):
        rsvopen.recover_keys({"code": bad, "cpoint": w.cpoint(), "cbox": w.cbox}, w.blob)


@pytest.mark.parametrize("data", [
    {"shares": [[1, "ab" * 16], [2, "cd" * 16]]},
    {"share": [1, "ab" * 16], "code": "AAAA-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA"},
    {"share": [1, "ab" * 16]},
], ids=["shares", "share_and_code", "share_alone"])
def test_the_inputs_of_the_line_of_m_are_gone(data):
    """Прямой самого `M` нет: две доли или доля + код — не вход; причина — одна строка
    о том, чего нет, а не разбор долей."""
    w = _World()
    with pytest.raises(rsvopen.OpenError, match="нет ни точек ключа кворума, ни кода с ящиком, ни ключа пакета"):
        rsvopen.recover_keys(data, w.blob)


def test_a_real_code_in_the_old_share_input_does_not_open():
    """Верный код прямой `Mc` и верная точка резерва в прежнем входе (share + code) без
    ящика — отказ: без ящика кода `M` не собрать."""
    w = _World()
    with pytest.raises(rsvopen.OpenError, match="нет ни точек"):
        rsvopen.recover_keys({"share": w.cpoint(), "code": w.c.code()}, w.blob)


def test_a_partial_quorum_input_is_not_taken_for_a_full_one():
    """Код без ящика кода (точка есть) — не вход кода под кворумом и не прежний вход."""
    w = _World()
    with pytest.raises(rsvopen.OpenError):
        rsvopen.recover_keys({"code": w.c.code(), "cpoint": w.cpoint()}, w.blob)
    with pytest.raises(rsvopen.OpenError):
        rsvopen.recover_keys({"qpoints": [w.qpoint(1), w.qpoint(2)]}, w.blob)
