"""uplink.py — такт аплинка на стороне клиента канала: восстановление прерванной транзакции и аварийное правило для висящего вопроса."""
from __future__ import annotations

import asyncio

from awgbot.runtime import linkclient as pkg          # крючок уведомления — на пакете
from .common import log


class UplinkMixin:
    async def uplink_tick(self) -> None:
        """Такт: прерванная перезапуском транзакция аплинка — назад; вопрос об
        аплинке висит, а агент сам видит аплинк мёртвым 10 минут — аварийно,
        ответ серверу — из очереди (сессия канала по линку от аплинка не зависит)."""
        res = await asyncio.to_thread(self.services.cfg_uplink_tick)
        if res.get("answer"):
            try:
                await pkg.report_cfg_answer(self.services, res["answer"])
            except Exception as e:                        # noqa: BLE001
                log.warning("канал линка: ответ серверу об аплинке не ушёл: %s", e)
        for note in res.get("notes") or ():
            if pkg._notify is None:
                break
            try:
                await pkg._notify(note)
            except Exception as e:                        # noqa: BLE001
                log.warning("канал линка: итог аплинка не ушёл в чат: %s", e)
