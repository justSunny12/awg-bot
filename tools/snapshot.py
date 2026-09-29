#!/usr/bin/env python3
"""
snapshot.py — снимок состояния awg-bot одним архивом, тем же составом, что
копия из чата: БД, conf/*.yaml, env, конфиги awg-интерфейсов и — у шлюза —
firewall.env и личные списки локальной сети.

Зовёт `awg-bot backup` и `awg-bot restore` (снимок ДО восстановления). Сборщик
один на все случаи нарочно: три сборки с разным составом однажды дали снимок
«до восстановления» без конфигов интерфейсов, и «вернуться» возвращало базу,
а не устройства. БД снимается через backup API SQLite — согласованно и на
работающем боте (cp терял WAL).

Запуск:  python -m tools.snapshot --out /path/to/snapshot.tgz
Коды выхода: 0 — снят, 1 — не снят (причина в stderr).
"""
from __future__ import annotations

import argparse
import os
import sys

from awgbot.core import config
from awgbot.domain.backupcrypto import BackupCryptoMixin


class _Snapshot(BackupCryptoMixin):
    """Ровно то, что нужно сборщику: база (для списка шлюзов сервера) и роль."""

    def __init__(self, db) -> None:
        self.db = db


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="снимок состояния awg-bot (открытый tgz)")
    ap.add_argument("--out", required=True, help="куда положить архив")
    args = ap.parse_args(argv)
    db = None
    try:
        if config.ROLE != "gateway" and os.path.exists(config.DB_PATH):
            from awgbot.infra.db import Database
            db = Database(config.DB_PATH)
        snap = _Snapshot(db)
        raw = snap.build_backup_archive(snap.backup_extra())
    except Exception as e:                            # noqa: BLE001
        print(f"снимок не снялся: {e}", file=sys.stderr)
        return 1
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:                         # noqa: BLE001
                pass
    tmp = f"{args.out}.part"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.replace(tmp, args.out)
    except OSError as e:
        print(f"снимок не записан: {e}", file=sys.stderr)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
