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
import time

from awgbot.core import config
from awgbot.util import timeutil

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

    # ── единый архив ─────────────────────────────────────────────────────────
    #
    # Одна резервная копия — один файл, который разворачивает `awg-bot restore`
    # на любом хосте: state/bot.db, state/conf/*.yaml (все настройки, из UI и
    # не из UI), state/env (токен и id админа) и awg/<iface>.conf (конфиги
    # awg-интерфейсов с приватными ключами). Раскладка та же, что у снимка
    # `awg-bot backup`, чтобы restore понимал обоих.

    META_NAME = "state/backup-meta.json"

    @staticmethod
    def backup_role() -> str:
        return "gw" if config.ROLE == "gateway" else "main"

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
                add("state/bot.db", config.DB_PATH.read_bytes())
            except OSError:
                pass
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
            raise RuntimeError("нет секрета шифрования")
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

    # ── восстановление из файла, присланного в чат ───────────────────────────

    RESTORE_PENDING = "restore-pending.tgz"
    RESTORE_DONE = "restore-done.json"

    def inspect_backup(self, blob: bytes, filename: str = "") -> dict:
        """Расшифровать (если .enc) и прочитать метку. Возвращает
        {ok, role, created_at, error, plain}. Чужая роль — ok=False."""
        import io
        import json
        import tarfile
        from awgbot.util import secrets_util
        plain = blob
        encrypted = filename.endswith(".enc") or (getattr(secrets_util, "MAGIC", None) and blob.startswith(secrets_util.MAGIC))
        if encrypted:
            kw = self.backup_enc_kwargs()
            if kw is None:
                return {"ok": False, "error": "копия шифрованная, а парольная фраза здесь не задана"}
            try:
                plain = secrets_util.decrypt(blob, **kw)
            except Exception:                              # noqa: BLE001
                return {"ok": False, "error": "не расшифровалась — парольная фраза не та"}
        try:
            with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
                f = tar.extractfile(self.META_NAME)
                meta = json.loads(f.read().decode()) if f else {}
        except Exception:                                  # noqa: BLE001
            return {"ok": False, "error": "это не резервная копия awg-bot"}
        role, created = str(meta.get("role") or ""), str(meta.get("created_at") or "")
        if not role or not created:
            return {"ok": False, "error": "в копии нет метки — она снята до версии 2.5.5"}
        if role != self.backup_role():
            who = "агента шлюза" if role == "gw" else "основного бота"
            return {"ok": False, "error": f"это копия {who}, а здесь {'агент шлюза' if self.backup_role() == 'gw' else 'основной бот'}"}
        return {"ok": True, "role": role, "created_at": created, "plain": plain}

    def prepare_restore(self, plain: bytes) -> str:
        """Расшифрованный архив — на диск, откуда его возьмёт awg-bot restore."""
        config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dst = config.BACKUP_DIR / self.RESTORE_PENDING
        dst.write_bytes(plain)
        dst.chmod(0o600)
        return str(dst)

    @staticmethod
    def launch_restore(path: str) -> None:
        """awg-bot restore --yes — ВНЕ нашего cgroup: он остановит сервис,
        подменит БД/конфиги и запустит бота заново; итог доложит новый процесс
        по маркеру restore-done.json."""
        import shutil
        import subprocess
        script = str(config.BASE_DIR / "awg-bot.sh")
        cmd = ["bash", script, "restore", "--yes", path]
        if shutil.which("systemd-run"):
            subprocess.Popen(["systemd-run", "--collect", "--quiet",
                              f"--unit=awg-bot-restore-{int(time.time())}", *cmd],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
        else:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)

    @staticmethod
    def pop_restore_done() -> dict | None:
        """Маркер завершённого восстановления (пишет awg-bot restore) — прочитать
        и убрать. В БД его хранить нельзя: БД как раз и подменяется."""
        import json
        p = config.DATA_DIR / BackupCryptoMixin.RESTORE_DONE
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            p.unlink()
        except OSError:
            pass
        return d
