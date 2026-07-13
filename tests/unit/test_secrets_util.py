"""Unit: awgbot.util.secrets_util — argon2id, SecretBox (крипто бэкапов)."""
import pytest

from awgbot.util import secrets_util as su

pytestmark = pytest.mark.unit


# ── отпечаток / идентификатор ключа ──────────────────────────────────────────




def test_decrypt_random_wrong_key_fails():
    blob = su.encrypt(b"x", key=su.gen_random_key())
    with pytest.raises(Exception):
        su.decrypt(blob, key=su.gen_random_key())


# ── шифрование бэкапов: пассфраза (соль в файле) ─────────────────────────────
def test_encrypt_decrypt_passphrase():
    blob = su.encrypt(b"secret-db-bytes", passphrase="my-strong-pass")
    assert su.inspect_mode(blob) == "passphrase"
    assert su.decrypt(blob, passphrase="my-strong-pass") == b"secret-db-bytes"


def test_decrypt_passphrase_wrong_fails():
    blob = su.encrypt(b"x", passphrase="right-pass")
    with pytest.raises(Exception):
        su.decrypt(blob, passphrase="wrong-pass")


def test_passphrase_blob_is_self_contained():
    # два шифрования одной фразой → разные соли → разные шифртексты, оба читаются
    b1 = su.encrypt(b"same", passphrase="p")
    b2 = su.encrypt(b"same", passphrase="p")
    assert b1 != b2
    assert su.decrypt(b1, passphrase="p") == su.decrypt(b2, passphrase="p") == b"same"


# ── валидация аргументов / формата ───────────────────────────────────────────
def test_encrypt_requires_exactly_one_secret():
    with pytest.raises(ValueError):
        su.encrypt(b"x")                                       # ни key, ни passphrase
    with pytest.raises(ValueError):
        su.encrypt(b"x", key=su.gen_random_key(), passphrase="p")


def test_inspect_mode_rejects_foreign_blob():
    with pytest.raises(ValueError):
        su.inspect_mode(b"NOT-OUR-MAGIC............")


def test_decrypt_mode_mismatch_needs_right_secret():
    blob_r = su.encrypt(b"x", key=su.gen_random_key())
    with pytest.raises(ValueError):
        su.decrypt(blob_r, passphrase="p")                    # random-файл, дали пассфразу
    blob_p = su.encrypt(b"x", passphrase="p")
    with pytest.raises(ValueError):
        su.decrypt(blob_p, key=su.gen_random_key())           # passphrase-файл, дали ключ
