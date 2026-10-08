"""Основа каталога снимков: Shot, константы детерминизма, общие построители состояния.

Сами снимки — по модулю на роль (shots_admin, shots_client, shots_gateway),
каталог их склеивает (catalog.py). Снимок — одно действие человека на экране
и всё, что бот на него ответил.
Роль снимка решает файл эталона (admin.txt, client.txt, guest.txt,
gateway.txt), диспетчер (у агента — свой) и того, кто жмёт: админ — у
admin и gateway, владелец профиля — у client.

Данные детерминированы: часы заморожены (NOW), версия в текстах — VERSION
каталога, а не установленная (иначе каждый выпуск правил бы эталоны), имя
бота — test_bot, хост агента — HOST, БД — свежая на каждый снимок.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Callable

from awgbot.core import config

VERSION = "1.2.3"
NOW = _dt.datetime(2026, 9, 15, 12, 0, 0, tzinfo=config.TZ)
HOST = "pi"
CLIENT_TG = 1000

# role снимка → роль диспетчера (routers_for)
DISPATCHER = {"admin": "client", "client": "client", "guest": "client", "gateway": "gateway"}


@dataclass(frozen=True)
class Shot:
    """id — «<роль>.<экран>[.<вариант>]»; press — колбэки по порядку (строка —
    уже упакованный, например кнопка прежней версии); start — payload
    команды /start («» — без payload), идёт первым; text — ввод после нажатий;
    conf — настройки поверх копии conf/ тестов; data — построитель состояния
    БД, возвращает (tg_id, имя) того, кто жмёт (None — админ)."""
    id: str
    role: str
    press: tuple = ()
    text: str | None = None
    conf: dict = field(default_factory=dict)
    start: str | None = None
    title: str = ""
    data: Callable | None = None

    def __post_init__(self):
        object.__setattr__(self, "press", tuple(self.press))
        assert self.role in DISPATCHER, self.role


# ── построители состояния ────────────────────────────────────────────────────

def owner(services):
    """Действующий клиент с годовой подпиской и без устройств."""
    created = services.create_client("Вася", 3, "year", 0)
    res = services.activate_client(created.invite_code, CLIENT_TG)
    assert res.ok, res.reason
    return CLIENT_TG, "Вася"
