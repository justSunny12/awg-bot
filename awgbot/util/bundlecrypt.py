"""
bundlecrypt.py — шифрование бандла шлюза для доставки через Telegram.

У ботов Telegram нет E2E: чат видят серверы и любой, кто получит доступ к
аккаунту. Внутри бандла — приватный ключ линка, кража которого делает
атакующего шлюзом (MITM всего RF-трафика). Поэтому файл в чате — шифртекст.

Пароль через Telegram не ходит: ключ выводится из ТЕКУЩЕГО приватного ключа
линка шлюза. Его знают обе стороны — ВПС сгенерировал и хранит в конфиге
шлюза, шлюз хранит в своём awglink.conf — и он никогда не ездил через чат
(первый бандл едет ногами, как и вся первая установка). Наблюдатель чата
видит только шифртекст; атакующий без старого ключа не расшифрует новый.

Примитивы — libsodium (PyNaCl уже в зависимостях ради бэкапов):
BLAKE2b как KDF, SecretBox (XSalsa20-Poly1305) как AEAD.
"""
from __future__ import annotations

import base64
import re

MAGIC = b"AWGGWB1\n"
_PERSON = b"awg-gw-bundle-v1"


def derive_key(privkey_b64: str) -> bytes:
    """32-байтовый ключ из приватного ключа линка (base64 из conf)."""
    from nacl.hash import blake2b
    from nacl.encoding import RawEncoder
    raw = base64.b64decode(privkey_b64.strip())
    if len(raw) != 32:
        raise ValueError("приватный ключ линка должен быть 32 байта")
    return blake2b(raw, digest_size=32, person=_PERSON, encoder=RawEncoder)


def encrypt(data: bytes, privkey_b64: str) -> bytes:
    from nacl.secret import SecretBox
    box = SecretBox(derive_key(privkey_b64))
    return MAGIC + box.encrypt(data)          # nonce внутри, случайный


def decrypt(blob: bytes, privkey_b64: str) -> bytes:
    """Открытый текст либо ValueError: не наш файл или не тот ключ. Различать
    эти два случая наружу незачем — ответ админу один: «пересобери бандл на
    ВПС», а детали ушли бы в чат, где им не место."""
    from nacl.secret import SecretBox
    from nacl.exceptions import CryptoError
    if not blob.startswith(MAGIC):
        raise ValueError("это не шифрованный бандл шлюза")
    try:
        return SecretBox(derive_key(privkey_b64)).decrypt(blob[len(MAGIC):])
    except CryptoError as e:
        raise ValueError("бандл не расшифровался — ключ линка не совпадает") from e


def read_privkey(conf_text: str) -> str:
    """PrivateKey из текста awg-конфига (первое вхождение, [Interface])."""
    m = re.search(r"^\s*PrivateKey\s*=\s*(\S+)", conf_text, re.M)
    if not m:
        raise ValueError("в конфиге нет PrivateKey")
    return m.group(1)
