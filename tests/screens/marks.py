"""Сторож меток «-n» в снимках экранов: ссылки на экраны и списки в
уведомлениях (всё, что ушло через notifier._send) — только с меткой, на
экранах и в итогах — только без неё.

Цена ошибки: ссылка уведомления без метки перерисовывает живое меню где-то
выше в чате (человек нажал — «ничего не произошло»); метка на экране
открывает экран с экрана новым сообщением, и меню в чате множатся.

record(mp) подменяет links.mark_new записью: его вывод — ровно тексты
уведомлений, отправленных за снимок; problems() сверяет с ними тела записи.
"""
from __future__ import annotations

import re

_START = re.compile(r'href="https://t\.me/[A-Za-z0-9_]+\?start=([A-Za-z0-9_-]+)"')


def record(mp) -> set[str]:
    """Подменить links.mark_new: множество его выводов пополняется по ходу."""
    from awgbot.bot import links
    seen: set[str] = set()
    real = links.mark_new

    def mark(text):
        out = real(text)
        if out:
            seen.add(out)
        return out
    mp.setattr(links, "mark_new", mark)
    return seen


def problems(shot_id: str, rec, notified: set[str]) -> list[str]:
    """Ссылки уведомлений без метки «-n» (или вне грамматики экранов) и ссылки
    на экраны с меткой — вне уведомлений."""
    from awgbot.bot import links
    out: list[str] = []
    for c in rec.calls:
        if not c.body:
            continue
        for p in _START.findall(c.body):
            if c.body in notified:
                if not p.endswith(links.NEW) and links.is_screen(p):
                    out.append(f"{shot_id} {c.head}: ссылка уведомления без метки «-n»: {p}")
                elif not p.endswith(links.NEW):
                    # параметр, которого грамматика экранов не знает: из
                    # уведомления он не откроется внизу чата (ни метки, ни экрана)
                    out.append(f"{shot_id} {c.head}: ссылка уведомления вне грамматики экранов: {p}")
            elif p.endswith(links.NEW) and links.is_screen(p[:-len(links.NEW)]):
                out.append(f"{shot_id} {c.head}: метка «-n» вне уведомления: {p}")
    return out
