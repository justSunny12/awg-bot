"""
inspect.py — часть пакета backup (см. __init__): разбор копии, присланной в
чат (inspect_backup), и план восстановления для вопроса.

Принимается только шифрованная копия сервера AWG (распознаётся по
содержимому, не по имени): метка схемы 2, версия не ниже версии
совместимости и не новее кода, то же поколение AmneziaWG, тот же ADMIN_ID
(из чата чужую копию не восстанавливают; из терминала — можно). Ответ несёт
план — тот же, что исполнит слияние в `awg-bot restore`. У шлюза этого
осмотра нет: его настройки — в бэкапе сервера AWG, копию в чате шлюза бот
убирает с объяснением.
"""
from __future__ import annotations

import logging

log = logging.getLogger("awgbot.backup")

OPEN_REFUSAL = "бэкап не зашифрован — восстанавливаются только шифрованные бэкапы (.tgz.enc)"


def _member(plain: bytes, name: str) -> bytes | None:
    import io
    import tarfile
    try:
        with tarfile.open(fileobj=io.BytesIO(plain), mode="r:*") as tar:
            f = tar.extractfile(name)
            return f.read() if f else None
    except Exception:                                  # noqa: BLE001 — не архив, нет файла
        return None


class BackupInspectMixin:
    def inspect_backup(self, blob: bytes, filename: str = "") -> dict:
        """Расшифровать, прочитать метку, проверить сторожа и построить план.
        {ok, created_at, plan (dict), error}; файл держит вызывающий сам —
        шифрованным (prepare_restore)."""
        from awgbot.util import secrets_util
        if not blob.startswith(secrets_util.MAGIC):
            return {"ok": False, "error": OPEN_REFUSAL}
        kw = self.backup_enc_kwargs()
        if kw is None:
            return {"ok": False, "error": ("бэкап шифрованный, а фраза здесь не задана — задай её в «🔐 Шифрование» "
                                           "и пришли файл снова")}
        try:
            plain = secrets_util.decrypt(blob, **kw)
        except Exception:                              # noqa: BLE001
            return {"ok": False, "error": "не расшифровалась — фраза не та"}
        from . import meta as _meta
        label = _meta.read(plain)
        err = _meta.check(label, admin_id_copy=_meta.env_admin_id(_member(plain, "state/env")), chat=True)
        if err:
            log.info("бэкап отвергнут: %s", err)
            return {"ok": False, "error": err}
        try:
            p = self._restore_plan(plain, label, getattr(getattr(self, "db", None), "path", None))
        except Exception as e:                         # noqa: BLE001 — база бэкапа не читается
            log.warning("план восстановления не построен: %s", e)
            return {"ok": False, "error": "база в бэкапе не читается"}
        return {"ok": True, "created_at": str(label.get("created_at") or ""), "plan": p.to_dict()}

    @staticmethod
    def _restore_plan(plain: bytes, label: dict, cur_db: str | None = None):
        """План по базе бэкапа (во временный каталог) и текущей базе."""
        import io
        import os
        import tarfile
        import tempfile
        from . import plan as _plan
        with tempfile.TemporaryDirectory(prefix="awg-restore-plan-") as d:
            state = os.path.join(d, "state")
            os.makedirs(os.path.join(state, "conf"))
            with tarfile.open(fileobj=io.BytesIO(plain), mode="r:*") as tar:
                for name in ("state/bot.db", "state/conf/app.yaml"):
                    f = None
                    try:
                        f = tar.extractfile(name)
                    except KeyError:
                        pass
                    if f is not None:
                        with open(os.path.join(d, name), "wb") as out:
                            out.write(f.read())
            if not os.path.exists(os.path.join(state, "bot.db")):
                raise ValueError("в бэкапе нет базы")
            return _plan.plan_for_dir(label, d, cur_db=cur_db)
