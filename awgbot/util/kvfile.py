"""Файл KEY=VALUE построчно (состояние ядра, статус скриптов обвязки)."""
from __future__ import annotations

from pathlib import Path


def read(path, *, strip_quotes: bool = False) -> dict:
    """Словарь из файла; нет файла — пусто. Комментарии и строки без «=»
    пропускаются; strip_quotes — снять двойные кавычки вокруг значения."""
    out: dict = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        out[k.strip()] = v.strip('"') if strip_quotes else v
    return out
