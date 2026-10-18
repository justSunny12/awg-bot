#!/usr/bin/env python3
"""
restore_release.py — переход на выпуск бэкапа для `awg-bot restore`, до
остановки сервиса (domain/fleet/restore.py): версия бэкапа та же — ничего;
другая — выпуск бэкапа с GitHub, sha256 ассета = digest метки.

    python -m tools.restore_release --meta DIR/state/backup-meta.json --out FILE [--archive ARCHIVE]

--archive — выпуск уже скачан ботом (файл из чата): сверяется только sha256
с digest метки, без сети. Иначе — проверка на GitHub и скачивание в --out.
Печать: первая строка — «same» или версия бэкапа; дальше — строка версии и
хвост снятого выпуска (Т-В1, без HTML) для терминала. Код 0 — годится; 1 —
отказ, причина (Т-В2) в stderr.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys


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
    if args.archive:
        from awgbot.core import config
        if fr.same_version(ver, config.INSTALLED_VERSION):
            print("same")
            return 0
        with open(args.archive, "rb") as f:
            have = hashlib.sha256(f.read()).hexdigest()
        if not digest.startswith("sha256:") or digest[7:] != have:
            print(f"архив выпуска v{ver} не совпал с бэкапом (sha256 другой) — переходить на него не буду",
                  file=sys.stderr)
            return 1
        if args.archive != args.out:
            shutil.copyfile(args.archive, args.out)
        print(ver)
        return 0
    rel = fr.plan_release(label)
    if rel["error"]:
        print(rel["error"], file=sys.stderr)
        return 1
    if rel["kind"] == fr.SAME:
        print("same")
        return 0
    try:
        path = fr.fetch_release(rel)
    except updates.UpdateError as e:
        print(str(e), file=sys.stderr)
        return 1
    shutil.move(path, args.out)
    print(ver)
    for line in fr.version_lines(rel):
        print(nb.plain(line))
    return 0


if __name__ == "__main__":
    sys.exit(main())
