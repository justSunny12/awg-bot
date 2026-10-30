"""Шина стенда вместо каналов шлюзов: гостевого (шлюз основы ↔ сторона зоны
резерва, в awgrsv) и канала конфигурации (шлюз ↔ свой сервер, в линке слота).
Линков слотов и awgrsv у стенда нет — слова ходят файлами в общем каталоге
пары (`$PL/bus/<получатель>/`), а доставку решает сеть стенда: шлюз-заглушка
(`gwnode`) пересылает и принимает слово, только пока ping до адреса сервера
проходит (рычаги net.sh режут и его).

Сообщение — JSON {"from": "A"|"B"|"G1"…, "slot": N, "ch": "guest"|"link",
"kind": вид, "body": {...}}; имя файла — время в наносекундах: порядок
доставки — порядок отправки.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path


def post(root: str | Path, dst: str, msg: dict) -> None:
    d = Path(root) / dst
    d.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns():020d}-{secrets.token_hex(3)}"
    tmp = d / f".{name}.tmp"
    tmp.write_text(json.dumps(msg, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, d / f"{name}.json")


def take(root: str | Path, me: str) -> list[dict]:
    d = Path(root) / me
    out: list[dict] = []
    try:
        names = sorted(p for p in d.iterdir() if p.suffix == ".json")
    except OSError:
        return out
    for p in names:
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        try:
            p.unlink()
        except OSError:
            pass
    return out
