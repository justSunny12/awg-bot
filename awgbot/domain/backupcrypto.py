"""
backupcrypto.py — секрет шифрования бэкапов, общий для обеих ролей.

Только парольная фраза, и только через чат: админ придумывает её сам и держит
вне хоста; бот принимает сообщением, удаляет его и никогда не показывает фразу
обратно — иначе она легла бы в тот же канал, куда приезжают копии, и
шифрование потеряло бы смысл. Хранение — в БД, как креды почты: на живом
хосте фраза защищена теми же правами, что и сама БД, а внутри копии она
зашифрована сама собой.

restore_backup.py на другом хосте снимает шифрование той же фразой или ключом.

Здесь же сборка копии (состав по роли) и разбор присланной в чат
(inspect_backup): не принимается открытая копия на шлюзе, не расшифрованная,
не копия awg-bot или без метки, копия чужой роли и копия со схемой базы ниже
минимума 3.2.0 (schema_gap — та же проверка, что tools/check_backup.py в
`awg-bot restore`).
"""
from __future__ import annotations

import logging
import time

from awgbot.core import config
from awgbot.util import timeutil


class BackupKeyMissing(RuntimeError):
    """Копия требует шифрования, а секрета нет (шлюз: внутри ключи линка)."""

log = logging.getLogger("awgbot.backupcrypto")

MIN_PASSPHRASE_LEN = 8


class BackupCryptoMixin:
    _BK_PASSPHRASE_KEY = "backup_passphrase"
    _BK_KEY_KEY = "backup_key"                 # base64 случайного ключа (прежняя схема, из env)

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
        self.db.set_state(self._BK_KEY_KEY, "")          # случайный ключ больше не действует

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
        if not encrypted and config.ROLE == "gateway":
            # копия шлюза всегда шифрованная (make_backup): открытый архив здесь
            # ничему легитимному не служит, а restore раскладывает его от root
            return {"ok": False, "error": "на шлюзе принимаются только шифрованные копии (.tgz.enc)"}
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
        gap = self._db_schema_gap(plain)
        if gap:
            # миграций ниже минимума в коде нет: такую базу нечем довести
            return {"ok": False, "error": f"копия снята версией ниже 3.2.0 ({gap}) — "
                                          "восстанови её на хосте с 3.2.0, потом обновись"}
        return {"ok": True, "role": role, "created_at": created, "plain": plain,
                "ifaces_changed": self._ifaces_changed(plain)}

    @staticmethod
    def _db_schema_gap(plain: bytes) -> str:
        """База из архива — во временный файл и под schema_gap; базы в архиве
        нет — проверять нечего."""
        import io
        import os
        import tarfile
        import tempfile
        from awgbot.infra.db.schema import schema_gap
        try:
            with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
                f = tar.extractfile("state/bot.db")
                raw = f.read() if f else None
        except Exception:                              # noqa: BLE001
            return ""
        if raw is None:
            return ""
        fd, tmp = tempfile.mkstemp(prefix="awg-restore-", suffix=".db")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(raw)
            return schema_gap(tmp)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    @staticmethod
    def iface_conf_dir() -> str:
        return config.GW_CONF_DIR if config.ROLE == "gateway" else config.AWG_DIR

    def _ifaces_changed(self, plain: bytes) -> list[str]:
        """Имена интерфейсов, чьи конфиги в копии отличаются от текущих: только
        их восстановление перезапишет и переподнимет. Не трогаем то, что не
        менялось с момента копии."""
        import io
        import os
        import tarfile
        out: list[str] = []
        try:
            with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
                for m in tar.getmembers():
                    if not (m.isfile() and m.name.startswith("awg/") and m.name.endswith(".conf")):
                        continue
                    raw = tar.extractfile(m).read()
                    cur = None
                    try:
                        with open(os.path.join(self.iface_conf_dir(), os.path.basename(m.name)), "rb") as f:
                            cur = f.read()
                    except OSError:
                        pass
                    if cur is None or cur.strip() != raw.strip():
                        out.append(os.path.basename(m.name)[:-len(".conf")])
        except Exception:                              # noqa: BLE001
            return out
        return out

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
        from awgbot.infra.detach import spawn_detached
        script = str(config.BASE_DIR / "awg-bot.sh")
        spawn_detached(["bash", script, "restore", "--yes", path], unit_prefix="awg-bot-restore")

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
