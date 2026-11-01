"""Файл и вывод KEY=VALUE построчно (состояние ядра, статус скриптов обвязки,
env бота и агента).

Разбор один на всех: вывод `--status` скриптов, `systemctl show`, awg.lock и
статус-файлы читались пятью копиями, и копии расходились в мелочах (комментарии,
пробел в ключе). Смена одной строки `KEY=` в env — тоже здесь: три копии
«прочие строки как были, своя — в конец» жили в токене агента, сопряжении и
токенах ботов шлюзов.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def parse(text: str, *, strip_quotes: bool = False) -> dict:
    """Словарь из текста KEY=VALUE. Пустые строки, комментарии (целая строка с
    «#»), строки без «=» и строки, где слева от «=» не ключ (пусто или пробел
    внутри — «ВНИМАНИЕ: …=…» скрипта), пропускаются; «=» в значении сохраняется;
    повтор ключа — последнее значение; strip_quotes — снять двойные кавычки
    вокруг значения."""
    out: dict = {}
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k or any(c.isspace() for c in k):
            continue
        out[k] = v.strip('"') if strip_quotes else v
    return out


def read(path, *, strip_quotes: bool = False) -> dict:
    """parse() файла; нет файла или не читается — пусто."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    return parse(text, strip_quotes=strip_quotes)


def line_value(text: str, key: str) -> str:
    """Значение первой строки `KEY=` текста (без пробелов по краям); нет — пусто.
    Строка ищется с начала, как её пишет set_line: «# KEY=…» и « KEY=…» — не она."""
    head = f"{key}="
    for line in str(text or "").splitlines():
        if line.startswith(head):
            return line[len(head):].strip()
    return ""


def get_line(path, key: str) -> str:
    """line_value() файла; нет файла или не читается — пусто."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    return line_value(text, key)


def set_line(path, key: str, value: Optional[str], *, mode: int = 0o600) -> bool:
    """Строку `KEY=value` — на место всех прежних `KEY=` (в конец файла), прочие
    строки как были; value None — строки ключа прочь. Запись атомарная, права
    mode с первого байта (util/fsatomic). Нет файла: с value — создаётся, без —
    ничего не пишется (False). Прочие ошибки ОС — исключение OSError: прежний
    файл цел."""
    from awgbot.util import fsatomic
    head = f"{key}="
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        if value is None:
            return False
        text = ""
    lines = [ln for ln in text.splitlines() if not ln.startswith(head)]
    if value is not None:
        lines.append(f"{head}{value}")
    fsatomic.write_private(path, "\n".join(lines) + "\n", mode)
    return True


__all__ = ["parse", "read", "line_value", "get_line", "set_line"]
