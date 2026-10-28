#!/usr/bin/env python3
"""
release_meta.py — метаданные релиза (awg-bot-release.json) для выпуска.

Инструмент выпуска (docs/RELEASING.md §2–§4), в поставку не идёт. Строит
метаданные из `awgbot/__version__.py`, `install/awg.lock` (поколение ядра) и
аргументов — адресатов, минимумов и условий на другую роль, которые выпуск
определяет по строкам коммитов; проверяет их той же схемой, что читают боты
(awgbot/infra/updates/meta.py). Файл — docs/releases/vX.Y.Z[.P].json; его
build_release.sh кладёт вторым артефактом, выпуск загружает рядом с поставкой.

    python -m tools.release_meta --main 3.2.0 --gw 3.2.0 --write
    python -m tools.release_meta --main 3.3.0 --needs-gw 3.3.0 --gw 3.3.0 --needs-main 3.3.0
    python -m tools.release_meta --hashtags docs/releases/v3.3.1.json
    python -m tools.release_meta --check docs/releases/v3.3.0.json --expect-version 3.3.0

--check читает файл так же, как бот (предел 16 КБ, UTF-8, схема); с
--expect-version сверяет ещё версию сборки и поколение ядра с
install/awg.lock. --check и --hashtags — взаимоисключающие.

--hashtags печатает машинные строки тела записи журнала — только для релизов
моложе 3.3.2 (переход: их читают боты 3.2.x); перекрёстных условий в теле нет
никогда. Код 0 — успех; 1 — метаданные не прошли проверку (причина в stderr);
2 — неверные аргументы.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _schema():
    """awgbot/infra/updates/meta.py — та же схема, что читают боты. Файлом, а
    не через пакет: пакет тянет конфиг (yaml), а сборке хватает python3."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "awg_release_meta_schema", ROOT / "awgbot" / "infra" / "updates" / "meta.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def installed_version(root: Path = ROOT) -> str:
    """Версия из awgbot/__version__.py — без импорта пакета."""
    text = (root / "awgbot" / "__version__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("в awgbot/__version__.py нет __version__")
    return m.group(1)


def awg_generation(root: Path = ROOT) -> int:
    """AWG_GENERATION из install/awg.lock."""
    text = (root / "install" / "awg.lock").read_text(encoding="utf-8")
    m = re.search(r"^AWG_GENERATION=(\d+)\s*$", text, re.M)
    if not m:
        raise SystemExit("в install/awg.lock нет AWG_GENERATION")
    return int(m.group(1))


def update_from() -> str:
    """UPDATE_FROM из awgbot/core/compat.py — той же строкой, что читает `update_from`
    в install/lib/update.sh (пакет не импортируем: тянет конфиг)."""
    src = (ROOT / "awgbot" / "core" / "compat.py").read_text(encoding="utf-8")
    mt = re.search(r'^UPDATE_FROM\s*=\s*"([^"]+)"', src, re.M)
    if not mt:
        raise ValueError("UPDATE_FROM не найден в awgbot/core/compat.py")
    return mt.group(1)


def build(version: str, gen: int, main: str | None = None, gw: str | None = None,
          needs_gw: str | None = None, needs_main: str | None = None) -> dict:
    """Метаданные схемы 1 (проверенные). Адресаты — те роли, чей минимум задан."""
    m = _schema()
    roles: dict = {}
    if main:
        roles["main"] = {"requires": main, **({"needs_gw": needs_gw} if needs_gw else {})}
    elif needs_gw:
        raise m.MetaError("needs_gw без --main: условие ставится только адресату")
    if gw:
        roles["gw"] = {"requires": gw, **({"needs_main": needs_main} if needs_main else {})}
    elif needs_main:
        raise m.MetaError("needs_main без --gw: условие ставится только адресату")
    meta = {"schema": m.SCHEMA, "version": version, "awg_gen": gen, "roles": roles}
    return m.check(meta, version)


def dump(meta: dict) -> str:
    return json.dumps(meta, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str]) -> int:
    m = _schema()
    ap = argparse.ArgumentParser(prog="release_meta", description="метаданные релиза")
    ap.add_argument("--version", help="версия релиза (по умолчанию — awgbot/__version__.py)")
    ap.add_argument("--main", help="минимум основного бота — релиз адресован ему")
    ap.add_argument("--gw", help="минимум агента шлюза — релиз адресован ему")
    ap.add_argument("--needs-gw", help="основному боту нужны шлюзы не ниже")
    ap.add_argument("--needs-main", help="агенту нужен сервер AWG не ниже")
    ap.add_argument("--write", action="store_true", help="записать docs/releases/vВЕРСИЯ.json")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", metavar="FILE", help="проверить готовый файл")
    mode.add_argument("--hashtags", metavar="FILE", help="строки тела записи журнала для файла")
    ap.add_argument("--expect-version",
                    help="с --check: версия обязана совпасть, поколение — с install/awg.lock")
    try:
        args = ap.parse_args(argv[1:])
    except SystemExit:
        return 2
    try:
        if args.check or args.hashtags:
            path = Path(args.check or args.hashtags)
            meta = m.parse(path.read_bytes(), path.stem)  # как читает бот: предел, UTF-8, схема
            if args.expect_version:
                if meta["version"] != args.expect_version:
                    raise m.MetaError(f"версия {meta['version']}, а собирается {args.expect_version}")
                gen = awg_generation()
                if meta["awg_gen"] != gen:
                    raise m.MetaError(f"поколение ядра {meta['awg_gen']}, а в install/awg.lock — {gen}")
            if args.hashtags:
                for line in m.body_lines(meta):
                    print(line)
            return 0
        if not (args.main or args.gw):
            print("нужен хотя бы один адресат: --main и/или --gw", file=sys.stderr)
            return 2
        version = args.version or installed_version()
        floor = update_from()
        for role, req in (("main", args.main), ("gw", args.gw)):
            if req and req != floor:
                raise ValueError(f"минимум {role} {req} ≠ UPDATE_FROM {floor} (awgbot/core/compat.py): сторож "
                                  "терминала и цепочка бота разошлись бы — сдвиг минимума ставит оба")
        meta = build(version, awg_generation(), args.main, args.gw, args.needs_gw, args.needs_main)
    except (m.MetaError, ValueError, OSError) as e:
        print(f"метаданные релиза: {e}", file=sys.stderr)
        return 1
    if args.write:
        out = ROOT / "docs" / "releases" / f"v{version}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(dump(meta), encoding="utf-8")
        print(out)
    else:
        sys.stdout.write(dump(meta))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
