"""
inspect.py — часть пакета backup (см. __init__): разбор копии, присланной в
чат (inspect_backup). Не принимается открытая копия на шлюзе, не
расшифрованная, не копия awg-bot или без метки, копия чужой роли и копия со
схемой базы ниже минимума 3.3.0 (schema_gap — та же проверка, что
tools/check_backup.py в `awg-bot restore`).
"""
from __future__ import annotations

import logging

from awgbot.core import config

log = logging.getLogger("awgbot.backup")


class BackupInspectMixin:
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
            return {"ok": False, "error": "на шлюзе принимаются только шифрованные бэкапы (.tgz.enc)"}
        if encrypted:
            kw = self.backup_enc_kwargs()
            if kw is None:
                return {"ok": False, "error": ("бэкап шифрованный, а фраза здесь не задана — задай её в «🔐 Шифрование» "
                                                      "и пришли файл снова")}
            try:
                plain = secrets_util.decrypt(blob, **kw)
            except Exception:                              # noqa: BLE001
                return {"ok": False, "error": "не расшифровалась — фраза не та"}
        try:
            with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
                f = tar.extractfile(self.META_NAME)
                meta = json.loads(f.read().decode()) if f else {}
        except Exception:                                  # noqa: BLE001
            return {"ok": False, "error": "это не бэкап awg-bot"}
        role, created = str(meta.get("role") or ""), str(meta.get("created_at") or "")
        if not role or not created:
            return {"ok": False, "error": "в бэкапе нет метки — он снят до v2.5.5"}
        if role != self.backup_role():
            # отказ говорит, куда нести файл: бэкап другой роли — в чат её бота
            if role == "gw":
                return {"ok": False, "error": "это бэкап шлюза — восстанови его в чате бота шлюза"}
            return {"ok": False, "error": "это бэкап сервера AWG — восстанови его в чате бота сервера AWG"}
        gap = self._db_schema_gap(plain)
        if gap:
            # миграций ниже минимума в коде нет: такую базу нечем довести;
            # подробность схемы (gap) — в журнал, не человеку
            log.info("бэкап отвергнут: схема ниже 3.3.0 (%s)", gap)
            where = "на шлюзе" if self.backup_role() == "gw" else "на сервере"
            return {"ok": False, "error": f"бэкап снят версией до v3.3.0 — восстанови его {where} "
                                          "с v3.3.0, затем обнови бота"}
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
