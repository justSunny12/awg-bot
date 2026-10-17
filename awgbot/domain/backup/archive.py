"""
archive.py — часть пакета backup (см. __init__): состав копии сервера AWG,
сборка архива и запись шифрованного файла.

Одна резервная копия — один шифрованный файл (*.tgz.enc), который
разворачивает `awg-bot restore` на любом хосте: метка state/backup-meta.json
(meta), state/bot.db, state/conf/*.yaml (все настройки, из UI и не из UI),
state/env (токены и id админа), awg/<iface>.conf (клиентский интерфейс и
сторона ВПС каждого линка) и root/gw-<линк>.conf (выданные шлюзовые стороны).
Сборщик один — копия из чата, по почте, по событию и `awg-bot backup`
(tools/snapshot). Открытых копий нет: без фразы файла нет. В каталоге копий
остаются последние KEEP_BACKUPS.
"""
from __future__ import annotations

import logging
import time

from awgbot.core import config
from awgbot.util import timeutil

from .crypto import BackupKeyMissing

log = logging.getLogger("awgbot.backup")


class BackupArchiveMixin:
    META_NAME = "state/backup-meta.json"

    @staticmethod
    def backup_role() -> str:
        """Роль в метке: копии бывают только у сервера AWG."""
        return "main"

    @staticmethod
    def db_snapshot_bytes(path) -> bytes | None:
        """Содержимое БД целиком, включая незачекпоинченный WAL: backup API
        SQLite снимает согласованную копию и на работающем боте. Побайтовое
        чтение файла отдавало базу без последних транзакций (устройства без
        ключей после восстановления). Нет файла — None."""
        import os
        import sqlite3
        import tempfile
        path = str(path)
        if not os.path.exists(path):
            return None
        fd, tmp = tempfile.mkstemp(prefix=".snap-", suffix=".db", dir=os.path.dirname(path) or None)
        os.close(fd)
        try:
            src = sqlite3.connect(path)
            try:
                dst = sqlite3.connect(tmp)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
            with open(tmp, "rb") as f:
                return f.read()
        finally:
            for suffix in ("", "-journal", "-wal", "-shm"):
                try:
                    os.unlink(tmp + suffix)
                except OSError:
                    pass

    def backup_extra(self) -> list[tuple[str, bytes]]:
        """Что кладём в копию сверх БД/conf/env. Один список на все случаи:
        копия из чата, по почте и `awg-bot backup` (tools/snapshot.py)."""
        import os
        extra: list[tuple[str, bytes]] = []

        def add_file(path: str, name: str) -> None:
            try:
                with open(path, "rb") as f:
                    extra.append((name, f.read()))
            except OSError:
                pass

        # конфиг awg-интерфейса (единственная копия вне сервера)
        from awgbot.infra import awg
        try:
            conf = awg.read_file(config.CONF_PATH)
            extra.append((f"awg/{config.AWG_INTERFACE}.conf", conf.encode("utf-8")))
        except awg.AwgError:
            pass
        # линки шлюзов: без них новая машина не соберёт ни один бандл и не
        # примет ни одну сессию канала — восстановление кончалось бы переустановкой шлюзов
        from . import meta as _meta
        for gw in self.db.gateways():
            add_file(os.path.join(_meta.ROOT_DIR, f"gw-{gw.link_if}.conf"), f"root/gw-{gw.link_if}.conf")
            add_file(os.path.join(config.AWG_DIR, f"{gw.link_if}.conf"), f"awg/{gw.link_if}.conf")
        return extra

    def build_backup_archive(self, extra: list[tuple[str, bytes]] | None = None) -> bytes:
        import glob
        import io
        import json
        import os
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            def add(name: str, raw: bytes, mode: int = 0o600) -> None:
                ti = tarfile.TarInfo(name)
                ti.size = len(raw)
                ti.mode = mode
                ti.mtime = int(time.time())
                tar.addfile(ti, io.BytesIO(raw))
            # Метка ВНУТРИ архива (схема 2, опись шлюзов, линия базы): имя
            # файла не в счёт — его переименуют, а восстановление читает отсюда.
            from . import meta as _meta
            label = _meta.build(getattr(self, "db", None), self.backup_role())
            add(self.META_NAME, json.dumps(label, ensure_ascii=False).encode(), 0o644)
            try:
                raw_db = self.db_snapshot_bytes(config.DB_PATH)
            except Exception as e:                    # noqa: BLE001 — sqlite3.Error, OSError
                log.warning("копия: БД не снялась (%s) — архив без state/bot.db", e)
                raw_db = None
            if raw_db is not None:
                add("state/bot.db", raw_db)
            for p in sorted(glob.glob(os.path.join(str(config.CONF_DIR), "*.yaml"))):
                try:
                    add(f"state/conf/{os.path.basename(p)}", open(p, "rb").read(), 0o644)
                except OSError:
                    pass
            if config.ENV_PATH is not None:
                try:
                    add("state/env", config.ENV_PATH.read_bytes())
                except OSError:
                    pass
            for name, raw in (extra or []):
                add(name, raw)
        return buf.getvalue()

    def write_backup_archive(self, tag: str, extra: list[tuple[str, bytes]] | None = None) -> list[str]:
        """Собрать архив, зашифровать и положить в BACKUP_DIR. Открытых копий
        нет: без секрета — BackupKeyMissing, файла нет. Возвращает список из
        одного пути — вызывающие ждут список."""
        enc_kwargs = self.backup_enc_kwargs()
        if enc_kwargs is None:
            raise BackupKeyMissing("нет секрета шифрования")
        config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = timeutil.now().strftime("%Y%m%d_%H%M%S")
        raw = self.build_backup_archive(extra)
        from awgbot.util import secrets_util
        dst = config.BACKUP_DIR / f"awg-bot-backup-{tag}-{stamp}.tgz.enc"
        dst.write_bytes(secrets_util.encrypt(raw, **enc_kwargs))
        try:
            dst.chmod(0o600)
        except OSError:
            pass
        self._rotate_backups()
        return [str(dst)]

    KEEP_BACKUPS = 5

    @classmethod
    def _rotate_backups(cls) -> None:
        """В каталоге копий — последние KEEP_BACKUPS снятых ботом (файл из чата
        для восстановления не трогается)."""
        files = sorted(config.BACKUP_DIR.glob("awg-bot-backup-*.tgz.enc"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
        for old in files[cls.KEEP_BACKUPS:]:
            try:
                old.unlink()
            except OSError:
                pass
