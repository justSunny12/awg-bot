"""Сторож меток пояса в эталонах: у каждого времени, которое видит человек, —
смещение «(UTC+5)».

В тексте и всплывашке каждая строка, где есть время (часы и минуты, с
секундами или без), несёт сразу после последнего времени метку « (UTC…)»:
у моментов метка своя у каждого (первое время строки проверяется так же,
если за ним идёт метка), у повторяющегося времени — одна в конце группы
(«20:00–07:00 (UTC+5)», «1-е, 12:00 → 5-е, 03:00 (UTC+5)»). Не проверяются:
<code> и <pre> (адреса, команды, примеры ввода), <blockquote> без expandable
(текст релиза), кнопки (метку несёт текст того же сообщения). Исключения — по
id снимка: текст, который пишет сам админ (рассылка)."""
from __future__ import annotations

import re

_TIME = re.compile(r"(?<![\d:.])\d{1,2}:\d{2}(?::\d{2})?(?![\d:])")
_LABEL = re.compile(r" \(UTC(?:[+-]\d{1,2}(?::\d{2})?)?\)")
_SKIP = re.compile(r"<code>.*?</code>|<pre>.*?</pre>|<blockquote>.*?</blockquote>", re.S)
_TAG = re.compile(r"<[^>]+>")


def unlabeled(text: str) -> list[str]:
    """Строки, где за последним временем нет метки пояса."""
    out = []
    for line in _TAG.sub("", _SKIP.sub(" ", text or "")).splitlines():
        marked = _LABEL.sub("\0", line)          # «5:30» внутри «(UTC+5:30)» — не время
        found = list(_TIME.finditer(marked))
        if found and not marked.startswith("\0", found[-1].end()):
            out.append(line.strip())
    return out


def problems(shot_id: str, rec, exceptions: set[str]) -> list[str]:
    """Нарушения в записи снимка; снимки из exceptions не проверяются."""
    if shot_id in exceptions:
        return []
    out = []
    for c in rec.calls:
        for what, text in (("текст", c.body), ("всплывашка", c.toast)):
            for line in unlabeled(text or ""):
                out.append(f"{shot_id} {c.head}: время без метки пояса ({what}): {line}")
    return out


def seen(rec) -> bool:
    """Есть ли в записи время без метки — для сторожа отживших исключений."""
    return any(unlabeled(c.body or "") or unlabeled(c.toast or "") for c in rec.calls)
