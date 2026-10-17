"""
crypto.py — часть пакета backup (см. __init__): секрет шифрования копий.

Только парольная фраза, и только через чат: админ придумывает её сам и держит
вне хоста; бот принимает сообщением, удаляет его и никогда не показывает фразу
обратно — иначе она легла бы в тот же канал, куда приезжают копии, и
шифрование потеряло бы смысл. Хранение — в БД, как креды почты: на живом
хосте фраза защищена теми же правами, что и сама БД, а внутри копии она
зашифрована сама собой.

restore_backup.py на другом хосте снимает шифрование той же фразой.
"""
from __future__ import annotations


class BackupKeyMissing(RuntimeError):
    """Ключевая фраза не задана: открытых копий нет, копия не снимается."""


MIN_PASSPHRASE_LEN = 8


class BackupCryptoMixin:
    _BK_PASSPHRASE_KEY = "backup_passphrase"

    def backup_enc_kwargs(self) -> dict | None:
        """kwargs для secrets_util.encrypt или None — фраза не задана. Только
        фраза: случайный ключ прежней схемы шифрованием не считается, копии
        того режима и так ниже версии совместимости."""
        phrase = self.db.get_state(self._BK_PASSPHRASE_KEY) or ""
        return {"passphrase": phrase} if phrase else None

    def backup_encryption_enabled(self) -> bool:
        return self.backup_enc_kwargs() is not None

    def backup_encryption_mode(self) -> str:
        """"passphrase" | "" — для экрана."""
        return "passphrase" if self.db.get_state(self._BK_PASSPHRASE_KEY) else ""

    def backup_set_passphrase(self, phrase: str) -> None:
        phrase = (phrase or "").strip()
        if len(phrase) < MIN_PASSPHRASE_LEN:
            raise ValueError(f"ключевая фраза короче {MIN_PASSPHRASE_LEN} символов")
        self.db.set_state(self._BK_PASSPHRASE_KEY, phrase)
