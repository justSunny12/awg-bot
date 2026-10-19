"""join.py — часть пакета main (см. __init__): агент шлюза в режиме сопряжения
(подключение командой из бота сервера AWG).

Шлюз подключают командой: токена бота шлюза и ADMIN_ID у агента ещё нет —
их довезёт канал после верного кода. Пока так, процесс агента поднимает
только клиент канала и применение документа, без aiogram и планировщика;
ход и код — в файле хода (/run/awg-bot/join.status), его читает терминал
(`awg-bot join`). Раздел token документа пишет BOT_TOKEN и перезапускает
агента — уже полным.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os

from awgbot.core import config
from awgbot.runtime import main as pkg          # подменяемое тестами — через пакет

from .common import log


def join_pending() -> bool:
    """Агенту нужен режим сопряжения: роль gateway, токена бота нет, а файл
    сопряжения говорит, что сопряжение не доведено."""
    if config.ROLE != "gateway" or config.BOT_TOKEN:
        return False
    from awgbot.domain.gateway.join import JOIN_FILE
    try:
        with open(JOIN_FILE, encoding="utf-8") as f:
            st = json.loads(f.read() or "{}")
    except (OSError, ValueError):
        return False
    return isinstance(st, dict) and st.get("state") in ("pairing", "accepted", "linked", "refused")


async def run_join() -> None:
    """Режим сопряжения: БД, сервисы агента, клиент канала — до перезапуска."""
    from awgbot.domain.gateway import GatewayServices
    from awgbot.runtime import linkclient
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    db = pkg.Database(config.DB_PATH)
    db.init_schema()
    services = GatewayServices(db)
    st = services.join_state()
    if st.get("state") == "refused":
        log.info("gateway: сопряжение закрыто сервером AWG — жду новую команду подключения")
        await asyncio.Event().wait()
    services.join_status(state="waiting" if st.get("state") == "pairing" else st.get("state", ""))
    log.info("Агент шлюза запущен в режиме сопряжения (без Telegram)")
    try:
        linkclient.ensure(services)
        while True:
            await asyncio.sleep(60)
            with contextlib.suppress(Exception):
                await linkclient.on_tick(services)
    finally:
        await linkclient.shutdown()
        with contextlib.suppress(Exception):
            db.close()
