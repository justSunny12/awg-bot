"""inputs.py — разбор ввода человека, общий для обработчиков обеих ролей.

parse_uint — неотрицательное целое из текста сообщения или номера в
колбэке. str.isdigit() пропускает «²», «⑤» и арабско-индийские цифры, а int()
на них бросает ValueError — обработчик падал, и диалог FSM висел в состоянии
ввода; очень длинное число проходило проверку и падало OverflowError уже в
базе после state.clear(). Здесь принимаются только ASCII-цифры не длиннее
девяти знаков (int SQLite не переполнить даже умножением на байты в
гигабайте) и потолок по месту. Обработчики бота .isdigit() не зовут —
сторож tests/unit/test_inputs_parse_uint.py.
"""
from __future__ import annotations

import re

_UINT = re.compile(r"[0-9]{1,9}")

TRAFFIC_GB_MAX = 1_000_000      # лимит трафика, ГБ: больше — опечатка, не намерение
DAYS_MAX = 3650                 # дни паузы и продления: десять лет
PORT_MAX = 65535


def parse_uint(raw, lo: int = 0, hi: int | None = None) -> int | None:
    """Целое lo..hi из строки или None: не число, не ASCII-цифры, длиннее
    девяти знаков или вне границ. None → переспрос у вызывающего."""
    s = str(raw or "").strip()
    if not _UINT.fullmatch(s):
        return None
    value = int(s)
    if value < lo or (hi is not None and value > hi):
        return None
    return value
