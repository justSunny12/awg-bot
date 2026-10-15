"""rekey.py — смена ключей линка на стороне клиента канала: link_next, link_go, link_cancel, link_done, частый такт на время перехода."""
from __future__ import annotations

import asyncio
import contextlib

from awgbot.util import gwlink

from awgbot.runtime import linkclient as pkg          # крючки уведомления, правки и вопроса — на пакете
from .common import log

REPLACED = "🔑 Новые ключи линка: пришёл более новый запрос — он ниже"
WITHDRAWN = "🔑 Новые ключи линка: запрос отозван сервером AWG"
GO_FAILED = "⚠️ Новые ключи линка не поднялись — вернул прежние"


class RekeyMixin:
    async def _rekey_say(self, text: str, msg=None) -> None:
        """Итог перехода — правкой сообщения приглашения (если оно известно и
        правится), иначе отдельным сообщением."""
        if not text:
            return
        try:
            if msg and pkg._edit is not None and await pkg._edit(int(msg[0]), int(msg[1]), text):
                return
            if pkg._notify is not None:
                await pkg._notify(text)
        except Exception as e:                            # noqa: BLE001
            log.warning("канал линка: итог смены ключей не ушёл в чат: %s", e)

    async def _rekey_next(self, msg: dict) -> None:
        """Новые ключи с сервера: проверить, сохранить, спросить человека; сервер
        узнаёт состояние ответом link_ack (asked — ждём человека, declined — отказ)."""
        res = await asyncio.to_thread(self.services.rekey_next_in, msg)
        if res.get("reply"):
            await self._send("link_ack", res["reply"], pad=gwlink.PAD_DELTA)
        if res.get("replaced"):
            await self._rekey_say(REPLACED, res["replaced"])
        if res.get("ask") and pkg._ask is not None:
            try:
                await pkg._ask("link")
            except Exception as e:                        # noqa: BLE001
                log.warning("канал линка: приглашение о ключах линка не показано: %s", e)

    async def send_rekey_answer(self, reply: dict) -> bool:
        """Ответ человека на приглашение — серверу (ready / declined). Сессии нет —
        сервер повторит link_next с тем же id, и шлюз ответит своим состоянием."""
        if self._writer is None or not reply:
            return False
        return await self._send("link_ack", reply, pad=gwlink.PAD_DELTA)

    async def _rekey_go(self, msg: dict) -> None:
        """link_go: переключение. Вышло — сессия рвётся своей рукой: TCP по
        линку переживает down/up интерфейса, а фиксация на обеих сторонах
        ждёт новой сессии (hello новым ключом; ответ сервера в ней). Не вышло —
        отказ серверу с причиной и итог человеку."""
        res = await asyncio.to_thread(self.services.rekey_go, msg)
        if not res.get("ok"):
            await self._send("link_ack", {"id": str(msg.get("id") or ""), "state": "declined",
                                          "error": str(res.get("error") or "")[:300]}, pad=gwlink.PAD_DELTA)
            if not res.get("quiet"):                      # устаревший link_go — ничего не трогали
                await self._rekey_say(GO_FAILED, res.get("msg"))
            return
        self.rekey_kick()
        if self._writer is not None:
            log.info("канал линка: линк на новых ключах — переподключаюсь")
            with contextlib.suppress(Exception):
                self._writer.close()

    async def _rekey_cancel(self, msg: dict) -> None:
        res = await asyncio.to_thread(self.services.rekey_cancel, msg)
        if res is not None:
            await self._rekey_say(WITHDRAWN, res.get("msg"))

    async def _rekey_answered(self) -> None:
        """Первое подписанное слово сервера в новой сессии: закрепить выбор ключей
        и сказать об этом серверу и человеку."""
        res = await asyncio.to_thread(self.services.rekey_server_answered)
        if res.get("done"):
            await self._send("link_done", res["done"], pad=gwlink.PAD_DELTA)
        await self._rekey_say(res.get("note") or "", res.get("msg"))

    async def rekey_tick(self) -> None:
        """Такт: срок переключения (90 с) и поиск рабочего конфига (сутки)."""
        res = await asyncio.to_thread(self.services.rekey_tick)
        await self._rekey_say(res.get("note") or "", res.get("msg"))

    async def rekey_tick_monitor(self) -> None:
        """Такт монитора: переход ведёт свой цикл — второй такт в ту же
        секунду дал бы двойное «вернул прежние»; цикла нет — тикаем сами (и
        поднимаем цикл, если переход идёт)."""
        task = self.__dict__.get("_rekey_task")
        if task is not None and not task.done():
            return
        await self.rekey_tick()
        self.rekey_kick()

    def rekey_stop(self) -> None:
        task = self.__dict__.pop("_rekey_task", None)
        if task is not None and not task.done():
            task.cancel()

    def rekey_kick(self) -> None:
        """Частый такт, пока переход идёт (switched — 10 с, поиск — 30 с): иначе
        срок 90 с проверялся бы тиком монитора раз в 3 минуты, и шлюз
        возвращался бы позже, чем ВПС (180 с)."""
        task = self.__dict__.get("_rekey_task")
        if task is not None and not task.done():
            return
        try:
            self.__dict__["_rekey_task"] = asyncio.get_running_loop().create_task(self._rekey_loop())
        except RuntimeError:
            pass                                          # без цикла (тесты, синхронный вызов) — хватит тика

    async def _rekey_loop(self) -> None:
        while True:
            try:
                every = await asyncio.to_thread(self.services.rekey_tick_every)
            except Exception as e:                        # noqa: BLE001
                log.warning("канал линка: такт смены ключей: %s", e)
                return
            if not every:
                return
            await asyncio.sleep(every)
            try:
                await self.rekey_tick()
            except Exception as e:                        # noqa: BLE001
                log.warning("канал линка: такт смены ключей: %s", e)
