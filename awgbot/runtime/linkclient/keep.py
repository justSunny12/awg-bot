"""keep.py — копия настроек шлюза для бэкапа сервера AWG (domain/gateway/keep):
ответы сервера на hello (keep_ok / keep_none / keep_back), отправка своего
блока `keep`. Своё — только после ответа сервера в этой сессии и не пока
висит вопрос в чате; сервер без keep1 не отвечает — агент не шлёт вовсе."""
from __future__ import annotations

import asyncio

from awgbot.util import gwlink
from awgbot.runtime import linkclient as pkg          # крючки — на пакете: их ставит main

from .common import log

WITHDRAWN = "ℹ️ Вопрос снят: сервер AWG больше не предлагает эти настройки — свои настройки шлюза ушли ему"


class KeepMixin:
    _keep_ready = False                    # сервер в этой сессии ответил на hello о копии настроек

    async def _keep_reply(self, msg: dict) -> None:
        kind = msg.get("t")
        if kind in ("keep_ok", "keep_none"):
            # предлагать серверу нечего: висящий вопрос снят (иначе «вернуть»
            # применило бы снятое предложение, а своё не уходило бы до ответа)
            dropped = await asyncio.to_thread(self.services.keep_ask_drop)
            if dropped:
                await self._keep_withdrawn()
            first, self._keep_ready = not self._keep_ready, True
            if kind == "keep_ok":
                await asyncio.to_thread(self.services.keep_acked, str(msg.get("hash") or ""))
                if first or dropped:                      # ответ на hello: своё — если разошлось
                    await self.keep_push()
                return
            await self.keep_push(force=True)
            return
        res = await asyncio.to_thread(self.services.keep_back_in, msg)
        if res.get("note") and pkg._notify is not None:
            try:
                await pkg._notify(res["note"], silent=True)       # настройки встали сами — без звука
            except Exception as e:                        # noqa: BLE001
                log.warning("канал линка: уведомление о настройках с сервера не ушло: %s", e)
        if res.get("action") == "wait":
            self._keep_ready = False                      # вопрос уже в чате или не применилось — своё не шлём
            return
        if res.get("action") == "ask":
            self._keep_ready = False
            if pkg._ask is not None:
                try:
                    await pkg._ask("keep")
                except Exception as e:                    # noqa: BLE001
                    log.warning("канал линка: вопрос о настройках из бэкапа не показан: %s", e)
            return
        self._keep_ready = True
        await self.keep_push(force=True)

    async def _keep_withdrawn(self) -> None:
        """Снятый вопрос — финишер без кнопок на месте сообщения с вопросом."""
        msg = await asyncio.to_thread(self.services.keep_msg_pop)
        if not msg or pkg._edit is None:
            return
        try:
            await pkg._edit(int(msg[0]), int(msg[1]), WITHDRAWN)
        except Exception as e:                            # noqa: BLE001
            log.warning("канал линка: снятый вопрос о настройках не поправлен: %s", e)

    async def keep_push(self, *, force: bool = False) -> bool:
        """Отправить свой блок: настройки разошлись с подтверждёнными, вырос
        счёт линка (раз в ~6 часов) или force (ответ на keep_none, итог
        применения или вопроса)."""
        if self._writer is None or not self._keep_ready:
            return False
        if await asyncio.to_thread(self.services.keep_section):
            return False                                  # вопрос висит — ответ человека решит
        if not force and not await asyncio.to_thread(self.services.keep_due):
            return False
        block = await asyncio.to_thread(self.services.keep_block)
        if not await self._send("keep", block, pad=gwlink.PAD_DELTA):
            return False
        await asyncio.to_thread(self.services.keep_sent, block)
        return True

    async def keep_tick(self) -> None:
        await self.keep_push()
