"""
base.py — основание Services: конструктор, общие атрибуты и помощник
экранирования для текстов уведомлений.
"""
from __future__ import annotations


def _e(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


class ServicesBase:
    # username бота — для deep-link'ов в текстах (t.me/<bot>?start=…); main
    # кладёт его после getMe. Пусто — ссылки не рисуются, текст остаётся текстом.
    bot_username: str = ""

    def __init__(self, db):
        self.db = db
