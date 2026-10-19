"""
restore.py — часть пакета backup (см. __init__): копия из чата — на диск
шифрованной (restore-pending.tgz.enc, 0600: `awg-bot restore` расшифрует её
фразой из базы во временный каталог операции и удалит при любом исходе),
запуск `awg-bot restore` вне cgroup бота и маркер его итога.
"""
from __future__ import annotations

from awgbot.core import config


class BackupRestoreMixin:
    RESTORE_PENDING = "restore-pending.tgz.enc"
    RESTORE_DONE = "restore-done.json"

    RESTORE_PLAN = "restore-pending.plan.json"

    def prepare_restore(self, blob: bytes, plan: dict | None = None) -> str:
        """Шифрованный файл из чата — на диск, откуда его возьмёт awg-bot
        restore, и рядом — план из вопроса: после остановки сервиса слияние
        сверит его с текущим. Открытого содержимого вне временного каталога
        операции нет."""
        import json
        import os
        config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dst = config.BACKUP_DIR / self.RESTORE_PENDING
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        dst.chmod(0o600)
        plan_path = config.BACKUP_DIR / self.RESTORE_PLAN
        if plan:
            plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        elif plan_path.exists():
            plan_path.unlink()
        return str(dst)

    def prepare_restore_release(self, plan: dict | None) -> str:
        """Версия бэкапа другая — выпуск бэкапа с GitHub до запуска restore
        (sha256 = digest метки): путь к архиву; та же версия — пусто. Сеть или
        sha256 — UpdateError (кнопку можно нажать снова)."""
        from awgbot.domain.fleet import restore as fleet_restore
        rel = (plan or {}).get("release") or {}
        if not rel or rel.get("kind") in ("", fleet_restore.SAME):
            return ""
        label = {"version": rel.get("version"), "digest": rel.get("digest")}
        cache = self._update_meta_cache() if hasattr(self, "_update_meta_cache") else None
        from .meta import installed_digest
        full = fleet_restore.plan_release(label, meta_cache=cache,     # тот же выпуск, свежий ответ GitHub
                                          installed_release=bool(installed_digest(getattr(self, "db", None))))
        if full["error"]:
            from awgbot.infra import updates
            raise updates.UpdateError(full["error"])
        return fleet_restore.fetch_release(full)

    @staticmethod
    def launch_restore(path: str, release: str = "") -> None:
        """awg-bot restore --yes — ВНЕ нашего cgroup: он остановит сервис,
        подменит БД/конфиги и запустит бота заново; итог доложит новый процесс
        по маркеру restore-done.json. release — выпуск бэкапа, уже скачанный:
        переход на него до слияния."""
        from awgbot.infra.detach import spawn_detached
        script = str(config.BASE_DIR / "awg-bot.sh")
        argv = ["bash", script, "restore", "--yes"] + (["--release", release] if release else []) + [path]
        spawn_detached(argv, unit_prefix="awg-bot-restore")

    RESTORE_FAILED = "restore-failed.json"
    RESTORE_PRUNE = ".restore-prune"

    @staticmethod
    def restore_done_pending() -> bool:
        """Этот старт — первый после восстановления (маркер) или после отката
        прерванного (метка prune): DROP, наложенные restore по собранной базе,
        сверка снимет лишние. Метка отката — одноразовая."""
        prune = config.DATA_DIR / BackupRestoreMixin.RESTORE_PRUNE
        flagged = prune.exists()
        if flagged:
            try:
                prune.unlink()
            except OSError:
                pass
        return flagged or (config.DATA_DIR / BackupRestoreMixin.RESTORE_DONE).exists()

    @staticmethod
    def pop_restore_failed_info() -> dict:
        """Отказ восстановления из чата (пишет awg-bot restore): {"failed",
        "release" — выпуск бэкапа, на который не перешли, "from" — версия,
        которая работает}; нет — пусто."""
        import json
        p = config.DATA_DIR / BackupRestoreMixin.RESTORE_FAILED
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = None
        try:
            p.unlink()
        except OSError:
            pass
        return d if isinstance(d, dict) else {}

    @staticmethod
    def pop_restore_done() -> dict | None:
        """Маркер завершённого восстановления (пишет awg-bot restore) — прочитать
        и убрать. В БД его хранить нельзя: БД как раз и подменяется."""
        import json
        p = config.DATA_DIR / BackupRestoreMixin.RESTORE_DONE
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            p.unlink()
        except OSError:
            pass
        return d
