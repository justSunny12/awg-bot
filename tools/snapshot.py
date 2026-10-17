#!/usr/bin/env python3
"""
snapshot.py — `awg-bot backup`: бэкап сервера AWG одним шифрованным архивом,
тем же составом, что копия из чата: БД, conf/*.yaml, env, конфиги
awg-интерфейсов, линки слотов и выданные шлюзовые стороны (root/gw-*.conf),
метка схемы 2 с описью шлюзов. Сборщик один на все случаи: БД снимается через
backup API SQLite — согласованно и на работающем боте (cp терял WAL).

Открытых бэкапов нет: шифрование — ключевой фразой из базы; без неё отказ и
файла нет. На шлюзе бэкапов нет вовсе (их настройки — в бэкапе сервера AWG).

Запуск:  python -m tools.snapshot --out /path/to/awg-bot-backup-….tgz.enc
Коды выхода: 0 — снят; 1 — не снят (причина в stderr).
"""
from __future__ import annotations

import argparse
import os
import sys

from awgbot.core import config
from awgbot.domain.backup.archive import BackupArchiveMixin
from awgbot.domain.backup.crypto import BackupCryptoMixin

NO_PHRASE = ("без ключевой фразы бэкап не сделать: открытых бэкапов нет. Задай фразу в боте "
             "сервера AWG: «💾 Бэкапы» → «🔐 Шифрование»")
ON_GATEWAY = ("на шлюзе бэкапов нет: настройки шлюза — в бэкапе сервера AWG. Новое устройство ставится "
              "файлом конфигурации шлюза (sudo sh <файл> --install), остальное довезёт канал конфигурации")


class _Snapshot(BackupCryptoMixin, BackupArchiveMixin):
    """Ровно то, что нужно сборщику: база (секрет, опись шлюзов) и роль."""

    def __init__(self, db) -> None:
        self.db = db


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="бэкап сервера AWG (шифрованный tgz)")
    ap.add_argument("--out", required=True, help="куда положить архив")
    args = ap.parse_args(argv)
    if config.ROLE == "gateway":
        print(ON_GATEWAY, file=sys.stderr)
        return 1
    db = None
    try:
        if not os.path.exists(config.DB_PATH):
            print(NO_PHRASE, file=sys.stderr)
            return 1
        from awgbot.infra.db import Database
        db = Database(config.DB_PATH)
        db.init_schema()              # база прежней версии до первого старта сервиса — довести
        snap = _Snapshot(db)
        kw = snap.backup_enc_kwargs()
        if kw is None:
            print(NO_PHRASE, file=sys.stderr)
            return 1
        raw = snap.build_backup_archive(snap.backup_extra())
        from awgbot.util import secrets_util
        blob = secrets_util.encrypt(raw, **kw)
    except Exception as e:                            # noqa: BLE001
        print(f"бэкап не снялся: {e}", file=sys.stderr)
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
            f.write(blob)
        os.replace(tmp, args.out)
    except OSError as e:
        print(f"бэкап не записан: {e}", file=sys.stderr)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
