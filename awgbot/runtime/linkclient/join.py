"""join.py — сессия сопряжения на стороне клиента канала (§3.6 концепта
«единые обновления»): join_eph — код на экран терминала (через файл хода),
join_wrong — осталось попыток, join_refused — сопряжение закрыто (клиент
больше не подключается), join_ok — секреты (ADMIN_ID); дальше смена ключей
`auto=join` той же сессией (link_next → ready без вопроса, link_go).

После переключения клиент приходит постоянным ключом обычной сессией: роль,
документ (аплинк — правилом `fresh`, токен бота — перезапуск агента полным).
"""
from __future__ import annotations

import asyncio
import time

from awgbot.util import gwlink

from .common import log


class JoinMixin:
    def _join_dead(self) -> bool:
        """Сервер закрыл сопряжение — стучаться по токену бессмысленно."""
        getter = getattr(self.services, "join_state", None)
        return callable(getter) and getter().get("state") == "refused"

    async def _join_loop(self, reader, last_seq) -> None:
        """Сессия сопряжения: только её сообщения, без снимков и доставок."""
        while True:
            try:
                line = await reader.readline()
            except ValueError:
                break
            if not line:
                break
            self._io(rx=len(line))
            try:
                msg = gwlink.unpack(self._channel_key(), line, last_seq=last_seq, nonce=self._cn)
            except gwlink.ProtocolError as e:
                log.warning("канал линка: сопряжение — сообщение с ВПС отвергнуто: %s", e)
                break
            last_seq = int(msg.get("seq") or 0)
            self.services.channel.last_word = time.time()
            await self._handle(msg)
            if self._join_dead():
                break

    async def _join_in(self, msg: dict) -> None:
        kind = msg.get("t")
        if kind == "join_eph":
            await asyncio.to_thread(self.services.join_eph_in, msg)
        elif kind == "join_wrong":
            await asyncio.to_thread(self.services.join_wrong_in, msg)
        elif kind == "join_refused":
            await asyncio.to_thread(self.services.join_refused_in, msg)
        elif kind == "join_ok":
            await asyncio.to_thread(self.services.join_ok_in, msg)
