"""
backupcrypto.py — секрет шифрования бэкапов, общий для обеих ролей.

Только парольная фраза, и только через чат: админ придумывает её сам и держит
вне хоста; бот принимает сообщением, удаляет его и никогда не показывает фразу
обратно — иначе она легла бы в тот же канал, куда приезжают копии, и
шифрование потеряло бы смысл. Хранение — в БД, как креды почты: на живом
хосте фраза защищена теми же правами, что и сама БД, а внутри копии она
зашифрована сама собой.

Прежняя схема (BACKUP_KEY / BACKUP_PASSPHRASE в env) читается только ради
одноразового переезда; restore_backup.py на другом хосте работает как раньше.
"""
from __future__ import annotations

import logging

from awgbot.core import config

log = logging.getLogger("awgbot.backupcrypto")

MIN_PASSPHRASE_LEN = 8


class BackupCryptoMixin:
    _BK_PASSPHRASE_KEY = "backup_passphrase"
    _BK_KEY_KEY = "backup_key"                 # base64 случайного ключа из env (переезд)

    def backup_enc_kwargs(self) -> dict | None:
        """kwargs для secrets_util.encrypt или None — шифрование не задано.
        Фраза важнее ключа: детерминированнее для восстановления."""
        phrase = self.db.get_state(self._BK_PASSPHRASE_KEY) or ""
        if phrase:
            return {"passphrase": phrase}
        key = self.db.get_state(self._BK_KEY_KEY) or ""
        if key:
            from awgbot.util import secrets_util
            return {"key": secrets_util.b64d(key)}
        return None

    def backup_encryption_enabled(self) -> bool:
        return self.backup_enc_kwargs() is not None

    def backup_encryption_mode(self) -> str:
        """"passphrase" | "key" | "" — для экрана."""
        if self.db.get_state(self._BK_PASSPHRASE_KEY):
            return "passphrase"
        if self.db.get_state(self._BK_KEY_KEY):
            return "key"
        return ""

    def backup_set_passphrase(self, phrase: str) -> None:
        phrase = (phrase or "").strip()
        if len(phrase) < MIN_PASSPHRASE_LEN:
            raise ValueError(f"фраза короче {MIN_PASSPHRASE_LEN} символов")
        self.db.set_state(self._BK_PASSPHRASE_KEY, phrase)
        self.db.set_state(self._BK_KEY_KEY, "")          # ключ из env больше не действует

    def backup_import_env_once(self) -> bool:
        """BACKUP_PASSPHRASE / BACKUP_KEY из env → БД, только если в БД пусто."""
        if self.backup_encryption_enabled():
            return False
        if config.BACKUP_PASSPHRASE:
            self.db.set_state(self._BK_PASSPHRASE_KEY, config.BACKUP_PASSPHRASE)
        elif config.BACKUP_KEY:
            self.db.set_state(self._BK_KEY_KEY, config.BACKUP_KEY)
        else:
            return False
        log.info("бэкап: секрет шифрования перенесён из env в БД")
        return True

    @staticmethod
    def backup_env_leftover() -> bool:
        return bool(config.BACKUP_PASSPHRASE or config.BACKUP_KEY)
