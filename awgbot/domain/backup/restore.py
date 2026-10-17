"""
restore.py — часть пакета backup (см. __init__): расшифрованная копия — на
диск, запуск `awg-bot restore` вне cgroup бота и маркер его итога.
"""
from __future__ import annotations

from awgbot.core import config


class BackupRestoreMixin:
    RESTORE_PENDING = "restore-pending.tgz"
    RESTORE_DONE = "restore-done.json"

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
