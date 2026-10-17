"""
backup/ — резервная копия, общая для обеих ролей: секрет шифрования (crypto),
состав и сборка архива (archive), разбор присланной в чат копии (inspect),
подготовка и запуск `awg-bot restore` и его маркер (restore).

Части — миксины сервисного слоя по смыслу; BackupMixin собирает их для
Services и GatewayServices. Снимку `awg-bot backup` (tools/snapshot.py) нужен
только сборщик — он берёт BackupArchiveMixin напрямую.
"""
from __future__ import annotations

from .archive import BackupArchiveMixin
from .crypto import BackupCryptoMixin
from .inspect import BackupInspectMixin
from .restore import BackupRestoreMixin


class BackupMixin(BackupCryptoMixin, BackupArchiveMixin, BackupInspectMixin, BackupRestoreMixin):
    """Секрет, сборка, осмотр и восстановление копии — одним миксином."""


__all__ = ["BackupMixin"]
