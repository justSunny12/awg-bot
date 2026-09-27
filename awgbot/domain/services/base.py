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

    # ── ссылки в текстах уведомлений: имя объекта — переход на его экран ────
    # (/start <payload>; без username бота — просто имя)

    def _link(self, payload: str, label: str) -> str:
        if not self.bot_username:
            return _e(label)
        return f'<a href="https://t.me/{self.bot_username}?start={payload}">{_e(label)}</a>'

    def cl_link(self, client) -> str:
        """Имя профиля — ссылка на его карточку у админа (cl-<id>)."""
        if client is None:
            return "?"
        return self._link(f"cl-{int(client.id)}", client.name)

    def dev_link(self, dev) -> str:
        """Имя устройства — ссылка на его карточку (dev-<id>): у админа — своя,
        у клиента и гостя — своё или удерживаемое."""
        if dev is None:
            return "?"
        return self._link(f"dev-{int(dev.id)}", dev.name)

    def __init__(self, db):
        self.db = db
        # канал линка: сессии и байты по слоту — пишет runtime/linkserver,
        # читает зонд живости (домен не заглядывает в runtime)
        from awgbot.domain.channelstate import ChannelState
        self.channel = ChannelState()
