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
    bot_name: str = ""          # имя профиля бота (агент отдаёт его серверу снимком канала)

    def __init__(self, db):
        self.db = db
        # канал линка: сессии и байты по слоту — пишет runtime/linkserver,
        # читает зонд живости (домен не заглядывает в runtime)
        from awgbot.domain.channelstate import ChannelState
        self.channel = ChannelState()
