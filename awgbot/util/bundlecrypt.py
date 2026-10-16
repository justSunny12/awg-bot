"""
bundlecrypt.py — ключ из приватного ключа линка и распознавание прежнего файла.

derive_key выводит 32-байтовый ключ из ТЕКУЩЕГО приватного ключа линка
шлюза: его знают обе стороны (ВПС сгенерировал и хранит в конфиге шлюза,
шлюз — в своём awglink.conf), и он никогда не ездил через чат. На нём стоят
ключ канала (gwlink.channel_key) и подписи заявок (gwsign); строка
персонализации — прежняя, иначе ключи разошлись бы с уже выданными.

MAGIC — подпись шифрованного файла перевыпуска выпусков до 3.3.1: агент
узнаёт такой файл, пересланный из истории чата, и отказывает ему словами.

Примитив — libsodium (PyNaCl уже в зависимостях ради бэкапов): BLAKE2b как KDF.
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


def read_privkey(conf_text: str) -> str:
    """PrivateKey из текста awg-конфига (первое вхождение, [Interface])."""
    m = re.search(r"^\s*PrivateKey\s*=\s*(\S+)", conf_text, re.M)
    if not m:
        raise ValueError("в конфиге нет PrivateKey")
    return m.group(1)
