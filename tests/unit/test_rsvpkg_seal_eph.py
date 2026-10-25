"""Конверт ключа пакета к разовому ключу процесса открытия
(util/rsvpkg/seal.py: `new_eph`, `seal_k_to`, `open_sealed_k`): круг,
привязка к поколению пакета и идентификатору открытия, чужой ключ и порча,
размер конверта в потолке поля `sealed` канала пары.

Цена ошибки: конверт, который открывается чужой парой, — бот резерва (или
кто угодно с публичной частью из open.status) читает ключ пакета основы;
конверт без привязки к поколению или открытию — ключ одного пакета подходит
к другому запуску процесса; конверт длиннее потолка поля — `go` не проходит
словарь канала и переезд кнопкой не работает вовсе.
"""
from __future__ import annotations

import pytest

from awgbot.util import pairproto
from awgbot.util.rsvpkg import seal

pytestmark = pytest.mark.unit

OID = "0123456789abcdef"


@pytest.fixture()
def pair():
    return seal.new_eph()


def test_the_key_sealed_to_the_ephemeral_key_opens_back_only_with_its_private_part(pair):
    priv, pub = pair
    k = seal.new_k()
    sealed = seal.seal_k_to(pub, k, 7, OID)
    assert seal.open_sealed_k(priv, sealed, 7, OID) == k
    assert k.hex() not in sealed.hex() and k not in sealed, "ключ лежит в конверте открытым"


def test_each_ephemeral_pair_is_new_and_the_private_part_is_not_derivable_from_the_public_one():
    (priv1, pub1), (priv2, pub2) = seal.new_eph(), seal.new_eph()
    assert len(priv1) == 32 and len(pub1) == seal.EPH_LEN
    assert pub1 != pub2 and priv1 != priv2, "две пары процесса совпали — конверт к одному открытию подойдёт другому"
    assert priv1 != pub1


def test_two_envelopes_of_the_same_key_differ_so_the_channel_does_not_show_a_repeat(pair):
    _priv, pub = pair
    k = seal.new_k()
    assert seal.seal_k_to(pub, k, 7, OID) != seal.seal_k_to(pub, k, 7, OID)


def test_a_foreign_private_key_does_not_open_the_envelope(pair):
    _priv, pub = pair
    other_priv, _other_pub = seal.new_eph()
    sealed = seal.seal_k_to(pub, seal.new_k(), 7, OID)
    with pytest.raises(seal.PackageError, match="не тот ключ"):
        seal.open_sealed_k(other_priv, sealed, 7, OID)


@pytest.mark.parametrize("pkg_gen,why", [(6, "ключ от более нового поколения"), (8, "ключ от прошлого поколения")])
def test_a_foreign_package_generation_does_not_open_the_envelope(pair, pkg_gen, why):
    """Конверт к поколению 7 не открывает пакет 6 или 8; причина называет
    направление — админ видит, кто отстал: основа выслала старый ключ или
    резерв держит устаревший пакет."""
    priv, pub = pair
    sealed = seal.seal_k_to(pub, seal.new_k(), 7, OID)
    with pytest.raises(seal.PackageError, match=why):
        seal.open_sealed_k(priv, sealed, pkg_gen, OID)


def test_a_foreign_opening_id_does_not_open_the_envelope(pair):
    priv, pub = pair
    sealed = seal.seal_k_to(pub, seal.new_k(), 7, OID)
    with pytest.raises(seal.PackageError, match="не к этому процессу открытия"):
        seal.open_sealed_k(priv, sealed, 7, "ffffffffffffffff")


@pytest.mark.parametrize("where", [0, 40, -1])
def test_a_damaged_envelope_does_not_open(pair, where):
    priv, pub = pair
    sealed = bytearray(seal.seal_k_to(pub, seal.new_k(), 7, OID))
    sealed[where] ^= 0x01
    with pytest.raises(seal.PackageError):
        seal.open_sealed_k(priv, bytes(sealed), 7, OID)
    with pytest.raises(seal.PackageError):
        seal.open_sealed_k(priv, b"", 7, OID)


def test_a_key_of_the_wrong_length_or_an_ephemeral_key_of_the_wrong_length_is_refused_before_sealing(pair):
    _priv, pub = pair
    with pytest.raises(seal.PackageError, match="K не той длины"):
        seal.seal_k_to(pub, b"\x01" * 16, 7, OID)
    with pytest.raises(seal.PackageError, match="32 байта"):
        seal.seal_k_to(pub[:31], seal.new_k(), 7, OID)


@pytest.mark.parametrize("gen", [7, 10 ** 7, 2 ** 31 - 1])
def test_the_envelope_fits_the_ceiling_of_the_sealed_field_of_the_channel(pair, gen):
    """`go{sealed}` идёт hex-строкой; потолок поля в словаре канала — 384 —
    обязан вмещать конверт при любом поколении пакета, которое словарь
    пропускает в `gen` (целое без потолка)."""
    _priv, pub = pair
    sealed = seal.seal_k_to(pub, seal.new_k(), gen, OID)
    ceiling = pairproto.PAIR_KINDS["go"][1]["sealed"][1]
    assert len(sealed.hex()) <= ceiling, f"конверт {len(sealed.hex())} знаков, потолок поля {ceiling}"
    pairproto.check(pairproto.PAIR_PROTO, "go", {"gen": 1, "kind": "button", "sealed": sealed.hex(), "oid": OID},
                    pairproto.INVITER)
