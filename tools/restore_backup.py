#!/usr/bin/env python3
"""
restore_backup.py — расшифровка резервной копии awg-bot (awg-bot-backup-*.tgz.enc).

Резервная копия сервера AWG — один архив: метка, БД, conf/*.yaml, env,
конфиги интерфейсов и линков, всегда шифрованный ключевой фразой (*.tgz.enc);
этот скрипт снимает шифрование. Запускается
где угодно, где есть Python + PyNaCl. Режим определяет по заголовку файла:

  • пассфраза — спросит фразу (соль внутри файла, ключ выведется сам);
  • случайный ключ (копии прежних версий; восстановить их уже нельзя, только
    расшифровать) — возьмёт BACKUP_KEY из окружения, из --key или спросит;
  • --phrase-from-db — секрет из базы этой установки (копия из чата: бот её
    уже расшифровал этой фразой; `awg-bot restore` зовёт так сам).

Расшифрованный файл кладётся рядом, без «.enc» (или в путь из --out). Ничего
никуда сам не «накатывает» — только расшифровывает. Восстанавливает
`awg-bot restore <файл .tgz.enc>`: он сам зовёт этот скрипт и принимает только
шифрованный файл.

Примеры:
    python restore_backup.py awg-bot-backup-main-20261017_120000.tgz.enc
    python restore_backup.py --out /tmp/backup.tgz awg-bot-backup-main-*.tgz.enc
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


def _db_secret() -> dict:
    """Секрет из базы установки (фраза важнее ключа) — как его держит бот:
    копию из чата бот уже расшифровал этой же фразой. Только root: база
    закрыта правами."""
    from awgbot.core import config
    import sqlite3
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        def get(key: str) -> str:
            row = con.execute("SELECT value FROM server_state WHERE key = ?", (key,)).fetchone()
            return str(row[0]) if row is not None and row[0] else ""
        phrase = get("backup_passphrase")
    finally:
        con.close()
    if phrase:
        return {"passphrase": phrase}
    raise ValueError("в базе нет ключевой фразы")


def _decrypt_one(path: Path, *, key_b64: str | None, out_arg: str | None, from_db: bool = False) -> Path:
    blob = path.read_bytes()
    mode = su.inspect_mode(blob)              # ValueError на чужом формате
    if from_db:
        data = su.decrypt(blob, **_db_secret())
    elif mode == "passphrase":
        phrase = getpass.getpass(f"Пассфраза для {path.name}: ")
        data = su.decrypt(blob, passphrase=phrase)
    else:
        kb = key_b64 or os.environ.get("BACKUP_KEY") or ""
        if not kb:
            # подсказка — в терминал, не в stdout (его глушат), ключ — без эха
            kb = getpass.getpass(f"BACKUP_KEY (base64) для {path.name}: ").strip()
        data = su.decrypt(blob, key=su.b64d(kb))
    dst = _out_path(path, out_arg)
    dst.write_bytes(data)
    return dst


def main() -> None:
    ap = argparse.ArgumentParser(description="Расшифровка резервной копии awg-bot (*.tgz.enc)")
    ap.add_argument("files", nargs="+", help="зашифрованные файлы *.enc")
    ap.add_argument("--key", help="BACKUP_KEY (base64) для файлов со случайным ключом")
    ap.add_argument("--out", help="путь результата (только при одном входном файле)")
    ap.add_argument("--phrase-from-db", action="store_true",
                    help="фраза — из базы этой установки (копия из чата; только root)")
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
            dst = _decrypt_one(src, key_b64=args.key, out_arg=args.out, from_db=args.phrase_from_db)
        except Exception as e:                        # noqa: BLE001
            # неверная фраза/ключ/подделка → CryptoError; чужой формат → ValueError
            print(f"[ошибка] {src.name}: {e}", file=sys.stderr)
            continue
        print(f"[ок] {src.name} → {dst}")
        done.append(dst)

    if not done:
        sys.exit(1)
    print("\nРазложить по местам: sudo awg-bot restore <архив> (бот остановится сам).")


if __name__ == "__main__":
    main()
