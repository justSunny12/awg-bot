"""Ключ из приватного ключа линка: на нём стоят ключ канала и подписи заявок."""
import base64
import os

import pytest

from awgbot.util import bundlecrypt as bc

PRIV = base64.b64encode(os.urandom(32)).decode()
OTHER = base64.b64encode(os.urandom(32)).decode()


def test_the_key_is_the_same_on_both_sides_and_differs_between_links():
    """ВПС и шлюз выводят ключ из одного приватного ключа линка — ключи
    совпадают; у другого линка ключ другой."""
    key = bc.derive_key(PRIV)
    assert len(key) == 32 and bc.derive_key(PRIV + "\n") == key
    assert bc.derive_key(OTHER) != key


def test_a_key_of_the_wrong_length_is_refused():
    with pytest.raises(ValueError):
        bc.derive_key(base64.b64encode(b"short").decode())


def test_read_privkey_from_conf():
    conf = "[Interface]\nAddress = 10.99.99.2/30\nPrivateKey = " + PRIV + "\n[Peer]\n"
    assert bc.read_privkey(conf) == PRIV
    with pytest.raises(ValueError):
        bc.read_privkey("[Interface]\nAddress = x\n")
