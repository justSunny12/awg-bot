"""Код подтверждения сопряжения (util/joinsas.py, §2.5 концепта «единые
обновления»): обе стороны получают один код; другой эфемерный ключ — другой
код; ввод прощает руки; не тот формат — не попытка; сравнение constant-time;
секреты после кода — SecretBox ключом сопряжения.
"""
from __future__ import annotations

import inspect

import pytest

from awgbot.util import joinsas

pytestmark = pytest.mark.unit

JID = "0123456789abcdef"


def _both(slot: int = 2):
    g_priv, g_pub = joinsas.new_pair()
    s_priv, s_pub = joinsas.new_pair()
    kg = joinsas.shared(g_priv, s_pub)
    ks = joinsas.shared(s_priv, g_pub)
    return (joinsas.code(JID, slot, g_pub, s_pub, kg), joinsas.code(JID, slot, g_pub, s_pub, ks),
            joinsas.pair_key(JID, kg), joinsas.pair_key(JID, ks))


def test_both_sides_get_the_same_code_and_key():
    cg, cs, kg, ks = _both()
    assert cg == cs and kg == ks and len(cg) == 6 and all(c in joinsas.ALPHABET for c in cg)
    assert joinsas.show(cg) == f"{cg[:3]}-{cg[3:]}"


def test_another_ephemeral_key_gives_another_code():
    codes = {_both()[0] for _ in range(20)}
    assert len(codes) > 15


def test_the_slot_and_id_are_in_the_code():
    g_priv, g_pub = joinsas.new_pair()
    _, s_pub = joinsas.new_pair()
    k = joinsas.shared(g_priv, s_pub)
    assert joinsas.code(JID, 1, g_pub, s_pub, k) != joinsas.code(JID, 2, g_pub, s_pub, k) \
        or joinsas.code(JID, 1, g_pub, s_pub, k) != joinsas.code("f" * 16, 1, g_pub, s_pub, k)


@pytest.mark.parametrize("text", ["k7f 2qx", "K7F-2QX", "K7F.2QX", "k7f_2qx", " K7F2QX "])
def test_input_forgives_hands(text):
    assert joinsas.normalize(text) == "K7F2QX"


def test_o_i_l_are_read_as_digits():
    assert joinsas.normalize("O1L-IOO") == "011100"


@pytest.mark.parametrize("text", ["", "K7F2Q", "K7F2QXX", "K7F-2Q!", "привет", "U7F2QX"])
def test_a_wrong_format_is_not_an_attempt(text):
    assert joinsas.normalize(text) is None


def test_comparison_is_constant_time():
    assert "compare_digest" in inspect.getsource(joinsas.same)
    assert joinsas.same("K7F2QX", "K7F2QX") and not joinsas.same("K7F2QX", "K7F2QY")


def test_secrets_are_sealed_with_the_pair_key():
    _, _, kg, ks = _both()
    blob = joinsas.seal(ks, {"admin_id": 4242, "bot": "srv_bot"})
    assert joinsas.unseal(kg, blob) == {"admin_id": 4242, "bot": "srv_bot"}
    with pytest.raises(ValueError):
        joinsas.unseal(bytes(32), blob)
