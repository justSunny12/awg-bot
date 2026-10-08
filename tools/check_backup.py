#!/usr/bin/env python3
"""
check_backup.py — схема базы из резервной копии не ниже минимума 3.2.0?

Зовётся из `awg-bot restore` на распакованной базе до остановки сервиса:
миграций под версии ниже минимума в коде нет, и базу старше нечем довести —
бот упал бы на первом запросе. Код 0 — схема годится; 1 — нет, причина в stderr.

    python -m tools.check_backup /tmp/x/state/bot.db
"""
from __future__ import annotations

import sys


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("использование: check_backup.py <bot.db>", file=sys.stderr)
        return 2
    from awgbot.infra.db.schema import schema_gap
    gap = schema_gap(argv[1])
    if gap:
        print(f"схема базы ниже минимума 3.2.0: {gap}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
