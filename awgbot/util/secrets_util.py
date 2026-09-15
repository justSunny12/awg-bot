"""
secrets_util.py — крипто-примитивы для секретов бота (без бизнес-логики).

Шифрование бэкапов — симметричный ключ (NaCl SecretBox = XSalsa20-Poly1305,
AEAD). Ключ либо случайный, либо выведен из пассфразы (argon2id). Задаётся с
экрана бота (Настройки → Резервное копирование), снимает — restore_backup.py.

Формат зашифрованного бэкапа (самоописываемый — restore не нужно знать режим
заранее):
    MAGIC(6) + mode(1) + [salt(16) если mode='P'] + SecretBox.encrypt(...)
где SecretBox.encrypt уже содержит nonce(24)+ciphertext+tag. mode: 'P' пассфраза
(соль внутри файла → та же фраза даёт тот же ключ где угодно), 'R' случайный ключ
(для расшифровки нужен сам ключ, админ хранит его отдельно).

Единственная тяжёлая зависимость — PyNaCl (libsodium). Ставится из requirements;
на awg-хосте для restore нужен тот же пакет (скрипт это проверяет и подсказывает).
"""
from __future__ import annotations

import base64

from nacl import pwhash, secret, utils

MAGIC = b"AWGBK1"                       # awg-bot backup, формат v1
_MODE_PASSPHRASE = b"P"
_MODE_RANDOM = b"R"

KEY_SIZE = secret.SecretBox.KEY_SIZE    # 32
_SALT_SIZE = pwhash.argon2id.SALTBYTES  # 16

# argon2id «moderate»: ~несколько сотен мс и ~256 МБ — разумно для разовых
# операций (бэкап 1/мес, restore руками), заметно дороже для брутфорса фразы.
_OPS = pwhash.argon2id.OPSLIMIT_MODERATE
_MEM = pwhash.argon2id.MEMLIMIT_MODERATE



def b64d(text: str) -> bytes:
    """base64 → байты ключа (так хранится случайный ключ в state «backup_key»)."""
    return base64.b64decode(text.strip().encode("ascii"))


# ─────────────────────────────────────────────────────────────────────────────
# Ключ шифрования бэкапов
# ─────────────────────────────────────────────────────────────────────────────

def gen_random_key() -> bytes:
    """Случайный 32-байтный ключ SecretBox. Из интерфейса режим 'R' больше не
    заводится (только фраза), но ключ, записанный прежней схемой, в БД старых
    установок остаётся — тест расшифровки таким ключом держим."""
    return utils.random(KEY_SIZE)


def derive_key(passphrase: str, salt: bytes) -> bytes:
    """Ключ SecretBox из пассфразы и соли (argon2id)."""
    return pwhash.argon2id.kdf(KEY_SIZE, passphrase.encode("utf-8"), salt,
                               opslimit=_OPS, memlimit=_MEM)


# ─────────────────────────────────────────────────────────────────────────────
# Шифрование / расшифровка бэкапа
# ─────────────────────────────────────────────────────────────────────────────

def encrypt(data: bytes, *, key: bytes | None = None,
            passphrase: str | None = None) -> bytes:
    """Зашифровать байты бэкапа. Ровно один из key/passphrase.
    passphrase → mode 'P' (свежая соль в файле); key → mode 'R'."""
    if bool(key) == bool(passphrase):
        raise ValueError("Задай ровно одно: key ИЛИ passphrase")
    if passphrase:
        salt = utils.random(_SALT_SIZE)
        box = secret.SecretBox(derive_key(passphrase, salt))
        return MAGIC + _MODE_PASSPHRASE + salt + box.encrypt(data)
    if len(key) != KEY_SIZE:
        raise ValueError(f"Ключ должен быть {KEY_SIZE} байт")
    box = secret.SecretBox(key)
    return MAGIC + _MODE_RANDOM + box.encrypt(data)


def inspect_mode(blob: bytes) -> str:
    """Режим зашифрованного файла: 'passphrase' | 'random'. ValueError на чужом
    формате (нет нашего MAGIC)."""
    if not blob.startswith(MAGIC):
        raise ValueError("Не наш формат бэкапа (нет сигнатуры AWGBK1)")
    mode = blob[len(MAGIC):len(MAGIC) + 1]
    if mode == _MODE_PASSPHRASE:
        return "passphrase"
    if mode == _MODE_RANDOM:
        return "random"
    raise ValueError("Неизвестный режим шифрования в заголовке")


def decrypt(blob: bytes, *, key: bytes | None = None,
            passphrase: str | None = None) -> bytes:
    """Расшифровать бэкап. Для mode 'P' нужна passphrase (соль берётся из файла),
    для 'R' — key. Поднимает ValueError/CryptoError на неверном ключе/подделке."""
    if not blob.startswith(MAGIC):
        raise ValueError("Не наш формат бэкапа (нет сигнатуры AWGBK1)")
    off = len(MAGIC)
    mode = blob[off:off + 1]
    off += 1
    if mode == _MODE_PASSPHRASE:
        if not passphrase:
            raise ValueError("Файл зашифрован пассфразой — нужна passphrase")
        salt = blob[off:off + _SALT_SIZE]
        ct = blob[off + _SALT_SIZE:]
        box = secret.SecretBox(derive_key(passphrase, salt))
        return box.decrypt(ct)
    if mode == _MODE_RANDOM:
        if not key:
            raise ValueError("Файл зашифрован случайным ключом — нужен key (BACKUP_KEY)")
        box = secret.SecretBox(key)
        return box.decrypt(blob[off:])
    raise ValueError("Неизвестный режим шифрования в заголовке")


__all__ = [
    "MAGIC", "KEY_SIZE",
    "b64d",
    "gen_random_key", "derive_key",
    "encrypt", "decrypt", "inspect_mode",
]
