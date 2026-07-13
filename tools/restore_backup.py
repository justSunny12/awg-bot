#!/usr/bin/env python3
"""
restore_backup.py — расшифровка бэкапов awg-bot (*.enc).

Запускается ГДЕ УГОДНО, где есть Python + PyNaCl (обычно на awg-хосте, куда вы
принесли зашифрованный бэкап из чата). Определяет режим по заголовку файла:

  • пассфраза — спросит фразу (соль внутри файла, ключ выведется сам);
  • случайный ключ — возьмёт BACKUP_KEY из окружения, из --key или спросит.

Расшифрованный файл кладётся рядом, без «.enc» (или в путь из --out).
Ничего никуда сам не «накатывает» — только расшифровывает. Как вернуть данные
на место, печатается подсказкой в конце (docker cp для awg0.conf/clientsTable,
подмена data/bot.db у остановленного бота).

Примеры:
    python restore_backup.py awg0_20260801_120000.conf.enc
    BACKUP_KEY=... python restore_backup.py bot_*.db.enc
    python restore_backup.py --key <base64> clientsTable_*.json.enc
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

try:
    from awgbot.util import secrets_util as su
except ModuleNotFoundError:
    sys.exit("Не найден secrets_util. Держите restore_backup.py рядом с ним "
             "(secrets_util.py) и установите PyNaCl: pip install pynacl")


def _out_path(src: Path, out_arg: str | None) -> Path:
    if out_arg:
        return Path(out_arg)
    if src.suffix == ".enc":
        return src.with_suffix("")            # снять только «.enc»
    return src.with_name(src.name + ".dec")


def _decrypt_one(path: Path, *, key_b64: str | None, out_arg: str | None) -> Path:
    blob = path.read_bytes()
    mode = su.inspect_mode(blob)              # ValueError на чужом формате
    if mode == "passphrase":
        phrase = getpass.getpass(f"Пассфраза для {path.name}: ")
        data = su.decrypt(blob, passphrase=phrase)
    else:
        kb = key_b64 or os.environ.get("BACKUP_KEY") or ""
        if not kb:
            kb = input(f"BACKUP_KEY (base64) для {path.name}: ").strip()
        data = su.decrypt(blob, key=su.b64d(kb))
    dst = _out_path(path, out_arg)
    dst.write_bytes(data)
    return dst


def main() -> None:
    ap = argparse.ArgumentParser(description="Расшифровка бэкапов awg-bot (*.enc)")
    ap.add_argument("files", nargs="+", help="зашифрованные файлы *.enc")
    ap.add_argument("--key", help="BACKUP_KEY (base64) для файлов со случайным ключом")
    ap.add_argument("--out", help="путь результата (только при одном входном файле)")
    args = ap.parse_args()

    if args.out and len(args.files) > 1:
        sys.exit("--out допустим только для одного файла")

    done: list[Path] = []
    for f in args.files:
        src = Path(f)
        if not src.exists():
            print(f"[пропуск] нет файла: {src}", file=sys.stderr)
            continue
        try:
            dst = _decrypt_one(src, key_b64=args.key, out_arg=args.out)
        except Exception as e:                        # noqa: BLE001
            # неверная фраза/ключ/подделка → CryptoError; чужой формат → ValueError
            print(f"[ошибка] {src.name}: {e}", file=sys.stderr)
            continue
        print(f"[ок] {src.name} → {dst}")
        done.append(dst)

    if not done:
        sys.exit(1)

    print("\nЧто дальше (в зависимости от файла):")
    print("  • awg0.conf      → docker cp awg0.conf amnezia-awg2:/opt/amnezia/awg/awg0.conf")
    print("                     затем внутри контейнера: awg-quick strip | awg syncconf")
    print("  • clientsTable   → docker cp clientsTable amnezia-awg2:/opt/amnezia/awg/clientsTable")
    print("  • bot.db         → остановите бота и подмените data/bot.db, затем запустите")


if __name__ == "__main__":
    main()
