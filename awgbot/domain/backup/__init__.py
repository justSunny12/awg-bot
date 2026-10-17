"""
backup/ — резервная копия сервера AWG (у шлюза копий нет — его настройки
лежат в ней же, domain/gwkeep): секрет шифрования (crypto), состав и сборка
шифрованного архива (archive), метка схемы 2 с описью шлюзов и её сторожа
(meta), разбор присланной в чат копии (inspect), план восстановления (plan),
слияние с текущим (merge/ — его исполняет tools/restore_merge), подготовка и
запуск `awg-bot restore` и его маркеры (restore).

Части — миксины сервисного слоя по смыслу; BackupMixin собирает их для
Services. Снимку `awg-bot backup` (tools/snapshot.py) нужны только сборщик и
секрет — он берёт BackupArchiveMixin и BackupCryptoMixin напрямую.
"""
from __future__ import annotations

from .archive import BackupArchiveMixin
from .crypto import BackupCryptoMixin
from .inspect import BackupInspectMixin
from .restore import BackupRestoreMixin


class BackupMixin(BackupCryptoMixin, BackupArchiveMixin, BackupInspectMixin, BackupRestoreMixin):
    """Секрет, сборка, осмотр и восстановление копии — одним миксином."""


__all__ = ["BackupMixin"]
