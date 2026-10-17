#!/usr/bin/env python3
"""
restore_merge.py — слияние распакованного бэкапа с текущим состоянием сервера
для `awg-bot restore` (domain/backup/merge).

    python -m tools.restore_merge --plan DIR          план восстановления текстом
                                                      (тот же, что вопрос в чате)
    python -m tools.restore_merge --copy DIR          слияние после остановки
                                                      сервиса: пишет только в DIR,
                                                      итог — DIR/restore-merge.json
    python -m tools.restore_merge --apply-blocks FILE DROP всем адресам из итога
                                                      (до подъёма интерфейсов и
                                                      ещё раз после, до старта
                                                      сервиса)

Коды выхода: 0 — сделано; 1 — нет (причина в stderr), живое не тронуто.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _plan(copy_dir: str) -> int:
    from awgbot.domain.backup import meta, plan
    from awgbot.domain.notices import backup as notices
    try:
        with open(os.path.join(copy_dir, meta.META_NAME), encoding="utf-8") as f:
            label = json.load(f)
    except (OSError, ValueError):
        label = None
    err = meta.check(label, chat=False)
    if err:
        print(err, file=sys.stderr)
        return 1
    try:
        p = plan.plan_for_dir(label, copy_dir)
    except Exception as e:                            # noqa: BLE001 — база бэкапа не читается
        print(f"план не построен: {e}", file=sys.stderr)
        return 1
    print(notices.plain(notices.restore_plan(p)))
    return 0


def _copy(copy_dir: str) -> int:
    from awgbot.domain.backup import merge
    try:
        merge.run(copy_dir)
    except merge.MergeError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


def _blocks(path: str) -> int:
    from awgbot.infra import awg
    try:
        with open(path, encoding="utf-8") as f:
            blocked = list(json.load(f).get("blocked") or [])
    except (OSError, ValueError) as e:
        print(f"итог слияния не прочитан: {e}", file=sys.stderr)
        return 1
    bad = 0
    for ip in blocked:
        try:
            awg.block_ip(str(ip))
        except (awg.AwgError, ValueError) as e:
            print(f"DROP {ip} не наложен: {e}", file=sys.stderr)
            bad += 1
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="слияние бэкапа с текущим состоянием сервера AWG")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", metavar="DIR")
    g.add_argument("--copy", metavar="DIR")
    g.add_argument("--apply-blocks", metavar="FILE")
    args = ap.parse_args(argv)
    if args.plan:
        return _plan(args.plan)
    if args.copy:
        return _copy(args.copy)
    return _blocks(args.apply_blocks)


if __name__ == "__main__":
    sys.exit(main())
