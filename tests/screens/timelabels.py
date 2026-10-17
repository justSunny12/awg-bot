"""Сторож меток пояса в эталонах: у каждого времени, которое видит человек, —
смещение «(UTC+5)».

Проверяется каждое время в тексте и всплывашке (часы и минуты, с секундами
или без): сразу за ним — метка « (UTC…)» (без тега между ними: метка внутри
той же ссылки, что и время), либо связка «–» / «→» к следующему времени той же
группы («20:00–07:00 (UTC+5)», «1-е, 12:00 → 5-е, 03:00 (UTC+5)»). Метка —
ожидаемого снимком пояса (по умолчанию пояс тестов, у снимков «другой пояс» —
свой); метка не у времени и не у имени пояса («до 12.10 (UTC+5)») — ошибка.
Не проверяются: <code> и <pre> (адреса, команды, примеры ввода), <blockquote>
без expandable (текст релиза), кнопки (метку несёт текст того же сообщения).
Исключения — строками по id снимка: текст, который пишет сам админ."""
from __future__ import annotations

import re

_TIME = re.compile(r"(?<![\d:.])\d{1,2}:\d{2}(?::\d{2})?(?![\d:])")
_LABEL = re.compile(r" \((UTC(?:[+-]\d{1,2}(?::\d{2})?)?)\)")
_SKIP = re.compile(r"<code>.*?</code>|<pre>.*?</pre>|<blockquote>.*?</blockquote>", re.S)
_TAG = re.compile(r"<[^>]+>")
_LINK = re.compile(r"^\s*(?:–|-|→)\s*(?:\d{1,2}-е,\s*)?$")          # связка к следующему времени группы
_ZONE_NAME = re.compile(r"(?:[A-Za-z_]+/[A-Za-z_+-]+|UTC[+-]?[\d:]*)$")   # «Asia/Yekaterinburg (UTC+5)»

TAG_MARK = "\x01"


def problems_in(text: str, expected: set[str]) -> list[str]:
    """Нарушения в тексте: строка и что с ней не так."""
    out = []
    for raw in _SKIP.sub(" ", text or "").splitlines():
        line = _TAG.sub(TAG_MARK, raw)                    # тег между временем и меткой — метка снаружи ссылки
        shown = line.replace(TAG_MARK, "").strip()
        labels = list(_LABEL.finditer(line))
        spans = [m.span() for m in labels]
        times = [m for m in _TIME.finditer(line) if not any(a <= m.start() < b for a, b in spans)]
        for k, t in enumerate(times):
            lab = _LABEL.match(line, t.end())
            if lab:
                if lab.group(1) not in expected:
                    out.append(f"метка {lab.group(1)} вместо {'/'.join(sorted(expected))}: {shown}")
                continue
            nxt = times[k + 1] if k + 1 < len(times) else None
            if nxt is None or not _LINK.match(line[t.end():nxt.start()]):
                out.append(f"время без метки пояса: {shown}")
        time_ends = {t.end() for t in times}
        for lab in labels:
            before = line[:lab.start()].replace(TAG_MARK, "")
            if lab.start() not in time_ends and not _ZONE_NAME.search(before):
                out.append(f"метка не у времени: {shown}")
    return out


def problems(shot_id: str, rec, exceptions: dict[str, set[str]], expected: set[str]) -> tuple[list[str], set[str]]:
    """(нарушения, сработавшие исключения) записи снимка: строка с подстрокой из
    exceptions[shot_id] не считается."""
    allowed = exceptions.get(shot_id, set())
    out, used = [], set()
    for c in rec.calls:
        for what, text in (("текст", c.body), ("всплывашка", c.toast)):
            for p in problems_in(text or "", expected):
                hit = {a for a in allowed if a in p}
                if hit:
                    used |= hit
                    continue
                out.append(f"{shot_id} {c.head} ({what}): {p}")
    return out, used
