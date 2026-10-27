"""
listlink.py — грамматика ссылок на экран «весь список»: параметр /start.

    list-<вид>[-<ref>][-n]
    ref: десятичное число (слот, ≤ 10⁶) или ключ снимка события [a-z0-9]{10}
    -n : открыть новым сообщением (ссылка из уведомления или следа события)

Вид — короткое имя [a-z]{2,10}; какие виды есть и кому они видны, знает
реестр экранов-списков (bot/lists). Самый длинный параметр — 28 знаков из 64,
алфавит Telegram [A-Za-z0-9_-]. Разбор — одной регуляркой: параметр /start —
ввод человека, всё, что не по грамматике, — не наша ссылка.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

PREFIX = "list-"
REF_MAX = 10 ** 6
_RE = re.compile(r"list-(?P<kind>[a-z]{2,10})(?:-(?P<ref>[0-9]{1,7}|[a-z0-9]{10}))?(?P<new>-n)?")
_KEY = re.compile(r"[a-z0-9]{10}")


@dataclass(frozen=True)
class Link:
    kind: str
    ref: int = 0
    key: str = ""
    new: bool = False


def build(kind: str, ref: int = 0, key: str = "", new: bool = False) -> str:
    """Параметр /start для вида: ref — слот (0 — без него), key — ключ снимка."""
    out = PREFIX + kind
    if key:
        out += "-" + key
    elif ref:
        out += f"-{int(ref)}"
    return out + ("-n" if new else "")


def parse(payload: str) -> Link | None:
    m = _RE.fullmatch(payload or "")
    if m is None:
        return None
    ref, key = 0, ""
    raw = m.group("ref") or ""
    if raw:
        if raw.isdigit() and len(raw) < 10:
            ref = int(raw)
            if not 0 < ref <= REF_MAX:
                return None
        elif _KEY.fullmatch(raw):
            key = raw
        else:
            return None
    return Link(kind=m.group("kind"), ref=ref, key=key, new=bool(m.group("new")))


def is_list(payload: str) -> bool:
    return parse(payload) is not None
