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


def test_fingerprint_names_one_file_the_same_way_on_both_sides():
    """Отпечаток — 16 hex от sha256 файла как он лежит в чате: сервер считает
    его при выдаче, агент — от полученного; по нему итог применения убирает
    из чата именно этот файл. Разные файлы — разные отпечатки."""
    blob = bc.encrypt(b"#!/bin/sh\necho hi\n", PRIV)
    fp = bc.fingerprint(blob)
    assert len(fp) == 16 and all(c in "0123456789abcdef" for c in fp), fp
    assert bc.fingerprint(bytes(blob)) == fp, "один файл — один отпечаток"
    assert bc.fingerprint(blob + b"\n") != fp
    assert bc.fingerprint(b"") == "e3b0c44298fc1c14", "пустой файл — отпечаток sha256 пустой строки"
