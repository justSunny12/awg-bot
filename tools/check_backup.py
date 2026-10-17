#!/usr/bin/env python3
"""
check_backup.py — сторож метки бэкапа для `awg-bot restore`, до остановки
сервиса: метка схемы 2, роль сервера, версия не ниже версии совместимости и
не новее кода, то же поколение AmneziaWG (domain/backup/meta.check).

    python -m tools.check_backup --meta /tmp/x/state/backup-meta.json

Код 0 — годится; 1 — нет, причина в stderr (без HTML).
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="сторож метки бэкапа awg-bot")
    ap.add_argument("--meta", required=True, help="state/backup-meta.json распакованного бэкапа")
    args = ap.parse_args(argv)
    from awgbot.domain.backup import meta
    try:
        with open(args.meta, encoding="utf-8") as f:
            label = json.load(f)
    except (OSError, ValueError):
        label = None
    err = meta.check(label, chat=False)
    if err:
        print(err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
