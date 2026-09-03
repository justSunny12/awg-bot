"""Шифрование бандла шлюза: ключ из приватного ключа линка, чат видит шифртекст."""
import base64
import os

import pytest

from awgbot.util import bundlecrypt as bc

PRIV = base64.b64encode(os.urandom(32)).decode()
OTHER = base64.b64encode(os.urandom(32)).decode()


def test_roundtrip_with_the_same_link_key():
    blob = bc.encrypt(b"#!/bin/sh\necho hi\n", PRIV)
    assert blob.startswith(bc.MAGIC)
    assert b"echo hi" not in blob, "открытый текст виден в чате"
    assert bc.decrypt(blob, PRIV) == b"#!/bin/sh\necho hi\n"


def test_wrong_link_key_is_rejected():
    """Ключ выводится из ТЕКУЩЕГО ключа линка: чужой шлюз (или шлюз, у которого
    ключ уже другой) бандл не откроет — и не должен."""
    blob = bc.encrypt(b"secret", PRIV)
    with pytest.raises(ValueError):
        bc.decrypt(blob, OTHER)


def test_foreign_file_is_rejected_before_any_crypto():
    with pytest.raises(ValueError):
        bc.decrypt(b"#!/bin/sh\n plain bundle", PRIV)


def test_nonce_makes_identical_bundles_differ():
    assert bc.encrypt(b"x", PRIV) != bc.encrypt(b"x", PRIV)


def test_read_privkey_from_conf():
    conf = "[Interface]\nAddress = 10.99.99.2/30\nPrivateKey = " + PRIV + "\n[Peer]\n"
    assert bc.read_privkey(conf) == PRIV
    with pytest.raises(ValueError):
        bc.read_privkey("[Interface]\nAddress = x\n")
