"""Запуск бота основы на резерве для стенда: как `python -m awgbot` в гостевом
режиме, но Bot API — макет `tests.lab.botapi` (адрес в LAB_BOTAPI), а не
настоящий Telegram.

Окружение — то же, что у юнита гостя: AWG_BOT_GUEST=1, AWG_BOT_CONF_DIR,
AWG_BOT_DATA_DIR, AWG_BOT_ENV, BOT_TOKEN и ADMIN_ID из env хранилища гостя.
LAB_MAKE_CLIENT=<имя> — до старта завести в базе гостя активный профиль (так
сценарий получает «запись, сделанную на резерве», не подключая настоящих
клиентов).

    ip netns exec awg-guest env … python -m tests.lab.guest_main
"""
from __future__ import annotations

import asyncio
import os

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer

import awgbot.runtime.main as main_pkg
from awgbot.core import config


class LabBot(main_pkg.Bot):
    def __init__(self, token: str, **kw):
        kw["session"] = AiohttpSession(api=TelegramAPIServer.from_base(os.environ["LAB_BOTAPI"]))
        super().__init__(token, **kw)


def _make_client(name: str) -> None:
    from awgbot.infra.db import Database
    db = Database(config.DB_PATH)
    db.init_schema()
    cid = db.create_client(name, 3, "2026-01-01T00:00:00+03:00", "2099-01-01T00:00:00+03:00", f"LAB-{name}")
    db.activate_client(cid, 880001)
    db.close()


def main() -> None:
    if not config.GUEST:
        raise SystemExit("AWG_BOT_GUEST не задан — это запуск гостевого режима")
    if os.environ.get("LAB_MAKE_CLIENT"):
        _make_client(os.environ["LAB_MAKE_CLIENT"])
    main_pkg.Bot = LabBot
    asyncio.run(main_pkg.run_guest())


if __name__ == "__main__":
    main()
