"""
archive.py — часть пакета backup (см. __init__): состав копии по роли, сборка
архива и запись шифрованного файла.

Одна резервная копия — один файл, который разворачивает `awg-bot restore` на
любом хосте: state/bot.db, state/conf/*.yaml (все настройки, из UI и не из
UI), state/env (токен и id админа) и awg/<iface>.conf (конфиги
awg-интерфейсов с приватными ключами). Раскладка та же, что у снимка
`awg-bot backup`, чтобы restore понимал обоих.
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
        return "gw" if config.ROLE == "gateway" else "main"

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
        """Что кладём в копию сверх БД/conf/env — по роли. Один список на все
        случаи: копия из чата, `awg-bot backup` и снимок перед восстановлением
        (tools/snapshot.py) — иначе снимок «до восстановления» шёл без конфигов
        интерфейсов, и «вернуться» возвращало базу, а не устройства."""
        import glob
        import os
        extra: list[tuple[str, bytes]] = []

        def add_file(path: str, name: str) -> None:
            try:
                with open(path, "rb") as f:
                    extra.append((name, f.read()))
            except OSError:
                pass

        if config.ROLE == "gateway" or getattr(self, "GATEWAY_ROLE", False):
            # все конфиги awg-интерфейсов шлюза (приватные ключи линка и туннеля —
            # единственная копия вне этой машины)
            for p in sorted(glob.glob(os.path.join(config.GW_CONF_DIR, "*.conf"))):
                add_file(p, f"awg/{os.path.basename(p)}")
            # локальное состояние файервола (порт, адреса снаружи, доверенные из
            # туннеля) — данные человека, бандл их не восстановит
            from awgbot.infra import gwguard
            add_file(gwguard.FW_ENV, "awg-gw/firewall.env")
            # личные списки локальной сети без VPN — тоже данные человека
            for name in gwguard.OWN_LIST_FILES:
                add_file(os.path.join(gwguard.DNSMASQ_D, name), f"awg-gw/lan/{name}")
            return extra
        # сервер: конфиг awg-интерфейса (единственная копия вне сервера)
        from awgbot.infra import awg
        try:
            conf = awg.read_file(config.CONF_PATH)
            extra.append((f"awg/{config.AWG_INTERFACE}.conf", conf.encode("utf-8")))
        except awg.AwgError:
            pass
        # линки шлюзов: без них новая машина не соберёт ни один бандл и не
        # примет ни одну сессию канала — восстановление кончалось бы переустановкой шлюзов
        for gw in self.db.gateways():
            add_file(f"/root/gw-{gw.link_if}.conf", f"root/gw-{gw.link_if}.conf")
            add_file(os.path.join(config.AWG_DIR, f"{gw.link_if}.conf"), f"awg/{gw.link_if}.conf")
        return extra

    def build_backup_archive(self, extra: list[tuple[str, bytes]] | None = None) -> bytes:
        import glob
        import io
        import json
        import os
        import socket
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            def add(name: str, raw: bytes, mode: int = 0o600) -> None:
                ti = tarfile.TarInfo(name)
                ti.size = len(raw)
                ti.mode = mode
                ti.mtime = int(time.time())
                tar.addfile(ti, io.BytesIO(raw))
            # Метка ВНУТРИ архива: чья копия и когда снята. Имя файла не в счёт —
            # его переименуют, а дату и роль восстановление читает отсюда.
            meta = {"role": self.backup_role(), "created_at": timeutil.now_iso(),
                    "hostname": socket.gethostname(), "version": config.INSTALLED_VERSION}
            add(self.META_NAME, json.dumps(meta, ensure_ascii=False).encode(), 0o644)
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

    def write_backup_archive(self, tag: str, extra: list[tuple[str, bytes]] | None = None,
                             *, require_encryption: bool = False) -> list[str]:
        """Собрать архив, зашифровать (если задан секрет) и положить в BACKUP_DIR.
        Возвращает список из одного пути — вызывающие ждут список."""
        enc_kwargs = self.backup_enc_kwargs()
        if enc_kwargs is None and require_encryption:
            raise BackupKeyMissing("нет секрета шифрования")
        config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = timeutil.now().strftime("%Y%m%d_%H%M%S")
        raw = self.build_backup_archive(extra)
        if enc_kwargs is not None:
            from awgbot.util import secrets_util
            dst = config.BACKUP_DIR / f"awg-bot-backup-{tag}-{stamp}.tgz.enc"
            dst.write_bytes(secrets_util.encrypt(raw, **enc_kwargs))
        else:
            dst = config.BACKUP_DIR / f"awg-bot-backup-{tag}-{stamp}.tgz"
            dst.write_bytes(raw)
        try:
            dst.chmod(0o600)
        except OSError:
            pass
        return [str(dst)]
