"""
crypto.py — часть пакета backup (см. __init__): секрет шифрования копий.

Только парольная фраза, и только через чат: админ придумывает её сам и держит
вне хоста; бот принимает сообщением, удаляет его и никогда не показывает фразу
обратно — иначе она легла бы в тот же канал, куда приезжают копии, и
шифрование потеряло бы смысл. Хранение — в БД, как креды почты: на живом
хосте фраза защищена теми же правами, что и сама БД, а внутри копии она
зашифрована сама собой.

restore_backup.py на другом хосте снимает шифрование той же фразой или ключом.
"""
from __future__ import annotations

from awgbot.core import config


class BackupKeyMissing(RuntimeError):
    """Копия требует шифрования, а секрета нет (шлюз: внутри ключи линка)."""


MIN_PASSPHRASE_LEN = 8


class BackupCryptoMixin:
    _BK_PASSPHRASE_KEY = "backup_passphrase"
    _BK_KEY_KEY = "backup_key"                 # base64 случайного ключа (прежняя схема, из env)

    def backup_enc_kwargs(self) -> dict | None:
        """kwargs для secrets_util.encrypt или None — шифрование не задано.
        Только фраза: случайный ключ прежней схемы (из env) шифрованием не
        считается — фраза обязательна, копии того режима и так ниже версии
        совместимости. Шлюз (до снятия его копий) — по-прежнему и ключом."""
        phrase = self.db.get_state(self._BK_PASSPHRASE_KEY) or ""
        if phrase:
            return {"passphrase": phrase}
        key = self.db.get_state(self._BK_KEY_KEY) or ""
        if key and (getattr(self, "GATEWAY_ROLE", False) or config.ROLE == "gateway"):
            from awgbot.util import secrets_util
            return {"key": secrets_util.b64d(key)}
        return None

    def backup_encryption_enabled(self) -> bool:
        return self.backup_enc_kwargs() is not None

    def backup_encryption_mode(self) -> str:
        """"passphrase" | "key" | "" — для экрана."""
        if self.db.get_state(self._BK_PASSPHRASE_KEY):
            return "passphrase"
        if self.db.get_state(self._BK_KEY_KEY) and self.backup_enc_kwargs() is not None:
            return "key"
        return ""

    def backup_set_passphrase(self, phrase: str) -> None:
        phrase = (phrase or "").strip()
        if len(phrase) < MIN_PASSPHRASE_LEN:
            raise ValueError(f"фраза короче {MIN_PASSPHRASE_LEN} символов")
        self.db.set_state(self._BK_PASSPHRASE_KEY, phrase)
        self.db.set_state(self._BK_KEY_KEY, "")          # случайный ключ больше не действует
