#!/usr/bin/env python3
"""
restore_release.py — переход на выпуск бэкапа для `awg-bot restore`, до
остановки сервиса (domain/fleet/restore.py): версия бэкапа та же — ничего;
другая — выпуск бэкапа с GitHub, sha256 ассета = digest метки.

    python -m tools.restore_release --meta DIR/state/backup-meta.json --out FILE [--archive ARCHIVE]

--archive — выпуск уже скачан ботом (файл из чата): сверяется только sha256
с digest метки, без сети. Иначе — проверка на GitHub и скачивание в --out.
Печать: первая строка — «same» или версия бэкапа; дальше — строка версии и
хвост снятого выпуска (без HTML) для терминала. Код 0 — годится; 1 —
отказ, причина словами в stderr.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys


def _installed_release() -> bool:
    """У установки есть digest выпуска (база бота): «та же версия» — строго."""
    from pathlib import Path
    from awgbot.core import config
    if not Path(config.DB_PATH).exists():
        return False
    try:
        from awgbot.domain.backup.meta import installed_digest
        from awgbot.infra.db import Database
        return bool(installed_digest(Database(config.DB_PATH)))
    except Exception:                                     # noqa: BLE001 — база не читается: как сборка не из выпуска
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="переход на выпуск бэкапа awg-bot")
    ap.add_argument("--meta", required=True, help="state/backup-meta.json распакованного бэкапа")
    ap.add_argument("--out", required=True, help="куда положить архив выпуска бэкапа")
    ap.add_argument("--archive", help="выпуск бэкапа, уже скачанный ботом")
    args = ap.parse_args(argv)
    from awgbot.domain.fleet import restore as fr
    from awgbot.domain.notices import backup as nb
    from awgbot.infra import updates
    try:
        with open(args.meta, encoding="utf-8") as f:
            label = json.load(f)
    except (OSError, ValueError):
        print("метка бэкапа не прочитана", file=sys.stderr)
        return 1
    ver = str(label.get("version") or "")
    digest = str(label.get("digest") or "")
    ours = _installed_release()
    if args.archive:
        from awgbot.core import config
        if fr.same_version(ver, config.INSTALLED_VERSION, releases=ours and bool(digest)):
            print("same")
            return 0
        try:
            with open(args.archive, "rb") as f:
                have = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            print(f"архив выпуска v{ver} не прочитан — скачай бэкап заново из чата бота", file=sys.stderr)
            return 1
        if not digest.startswith("sha256:") or digest[7:] != have:
            print(f"архив выпуска v{ver} не совпал с бэкапом (sha256 другой) — переходить на него не буду",
                  file=sys.stderr)
            return 1
        if args.archive != args.out:
            try:
                shutil.copyfile(args.archive, args.out)
            except OSError as e:
                print(f"архив выпуска v{ver} не скопирован: {e}", file=sys.stderr)
                return 1
        print(ver)
        return 0
    rel = fr.plan_release(label, installed_release=ours)
    if rel["error"]:
        print(rel["error"], file=sys.stderr)
        return 1
    if rel["kind"] == fr.SAME:
        print("same")
        return 0
    import os
    out = os.path.abspath(args.out)
    try:
        # сразу в --out (временный каталог операции): файл бота в каталоге
        # бэкапов (restore-pending.release.tgz) терминал не трогает
        fr.fetch_release(rel, dest_dir=os.path.dirname(out), name=os.path.basename(out))
    except (updates.UpdateError, OSError) as e:
        print(str(e), file=sys.stderr)
        return 1
    print(ver)
    for line in fr.version_lines(rel):
        print(nb.plain(line))
    return 0


if __name__ == "__main__":
    sys.exit(main())
