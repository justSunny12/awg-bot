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


GUEST_TG = 2000          # гость «Артём» (shots_client)

# подписи чатов-адресатов в записи («+ send #1 → чат админ»)
PEERS = {config.ADMIN_ID: "админ", CLIENT_TG: "клиент", GUEST_TG: "гость"}


@dataclass(frozen=True)
class Shot:
    """id — «<роль>.<экран>[.<вариант>]»; steps — шаги по порядку (формат —
    harness.check_step): ("start", payload), ("press", колбэк), ("text",
    ввод[, entities]), ("file", имя, байты[, подпись]), ("call", функция[,
    имя]). Сокращения, если порядок обычный: start — /start первым, press —
    колбэки (строка — уже упакованный, например кнопка прежней версии), text —
    ввод после нажатий, call — событие без действия человека последним
    (функция (services, bot) → корутина; или (имя, функция)). Печатается
    последний шаг. conf — настройки поверх копии conf/ тестов; data —
    построитель состояния (services[, monkeypatch снимка]) → (tg_id, имя) того,
    кто действует; photo — сообщение #1, под которым жмут, — фото."""
    id: str
    role: str
    press: tuple = ()
    text: str | None = None
    conf: dict = field(default_factory=dict)
    start: str | None = None
    title: str = ""
    data: Callable | None = None
    steps: tuple = ()
    call: object = None
    photo: bool = False

    def __post_init__(self):
        object.__setattr__(self, "press", tuple(self.press))
        object.__setattr__(self, "steps", tuple(tuple(s) for s in self.steps))
        assert self.role in DISPATCHER, self.role
        short = self.press or self.start is not None or self.text is not None or self.call is not None
        assert not (self.steps and short), f"{self.id}: steps и сокращения (press/start/text/call) вместе"

    def all_steps(self) -> tuple:
        """Шаги снимка: явные или развёрнутые из сокращений."""
        if self.steps:
            return self.steps
        out = []
        if self.start is not None:
            out.append(("start", self.start))
        out += [("press", p) for p in self.press]
        if self.text is not None:
            out.append(("text", self.text))
        if self.call is not None:
            out.append(("call", self.call[1], self.call[0]) if isinstance(self.call, tuple)
                       else ("call", self.call))
        return tuple(out)


# ── пояс показа на снимок ────────────────────────────────────────────────────

def zone(mp, name: str, *, source: str = "host", bad: str = "", host: str = "") -> None:
    """Пояс бота на один снимок: «агент в другом поясе», ключ timezone задан
    (source="conf", host — пояс хоста) или не распознан (source="conf_bad",
    bad — значение ключа). Часы снимка — те же (NOW), показ пересчитывается."""
    from zoneinfo import ZoneInfo
    from awgbot.util import timeutil
    tz = ZoneInfo(name)
    mp.setattr(config, "TZ", tz)
    mp.setattr(timeutil, "TZ", tz)
    mp.setattr(config, "TZ_NAME", name)
    mp.setattr(config, "TZ_SOURCE", source)
    mp.setattr(config, "TZ_HOST_NAME", host or name)
    mp.setattr(config, "TZ_BAD_VALUE", bad)


def in_zone(builder, name: str, **kw):
    """Построитель снимка в другом поясе: zone(), затем builder (services[, mp])
    или (без builder) админ."""
    import inspect

    def build(services, mp):
        zone(mp, name, **kw)
        if builder is None:
            return config.ADMIN_ID, "Админ"
        n = len([p for p in inspect.signature(builder).parameters.values()
                 if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)])
        return builder(services, mp) if n >= 2 else builder(services)
    build.zone = name                    # сторож меток ждёт смещение этого пояса
    return build


# ── построители состояния ────────────────────────────────────────────────────

def owner(services):
    """Действующий клиент с годовой подпиской и без устройств."""
    created = services.create_client("Вася", 3, "year", 0)
    res = services.activate_client(created.invite_code, CLIENT_TG)
    assert res.ok, res.reason
    return CLIENT_TG, "Вася"
