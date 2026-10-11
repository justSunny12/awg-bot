"""rekey.py — смена ключей линка на стороне слушателя: link_next, link_ack, link_go, переключение ВПС, фиксация hello, link_done, сроки, возврат на старте."""
from __future__ import annotations

import asyncio

from awgbot.util import gwlink

from awgbot.runtime import linkserver as pkg          # крючки уведомлений — на пакете
from .common import log

COMMIT_DELAY = 2.0      # с: link_go успевает уйти в сокет до переподъёма линка ВПС


class RekeyMixin:
    async def notes(self, notes) -> None:
        """Уведомления админу о событиях перехода — крючком, который ставит main."""
        if notes and pkg._on_notes is not None:
            try:
                await pkg._on_notes(list(notes))
            except Exception as e:                        # noqa: BLE001
                log.warning("канал линка: уведомления о смене ключей не ушли: %s", e)

    async def rekey_boot(self) -> None:
        """Возврат на старте — один раз за процесс, кто бы ни позвал первым
        (старт слушателя или такт живости); сервис сам держит флаг под замком."""
        if self.__dict__.get("_rekey_booted"):
            return
        self.__dict__["_rekey_booted"] = True
        await self.notes(await asyncio.to_thread(self.services.gateway_rekey_startup))

    async def deliver_rekey(self, slot_id: int) -> bool:
        """link_next слоту, если переход ждёт шлюза (staged/asked) и в этой сессии
        ещё не уходил: сессия упала между asked и ready — шлюз узнает тот же id и
        второй раз не спросит."""
        sess = self._sessions.get(slot_id)
        if sess is None or not sess.hello or "confirm1" not in sess.caps:
            return False
        # отзыв, ждавший сессии (сервер вернул прежние, а шлюз мог остаться в «согласии»)
        rid = await asyncio.to_thread(self.services.gateway_rekey_cancel_peek, slot_id)
        if rid and await self.send(slot_id, "link_cancel", {"id": rid}):
            await asyncio.to_thread(self.services.gateway_rekey_cancel_done, slot_id, rid)   # не ушёл — ждёт сессии
        body = await asyncio.to_thread(self.services.gateway_rekey_next_body, slot_id)
        if not body or sess.rekey_sent == body["id"]:
            return False
        sess.rekey_sent = body["id"]
        log.info("канал линка: слоту %s уходят новые ключи линка (%s)", slot_id, body["fp"])
        return await self.send(slot_id, "link_next", body, pad=gwlink.PAD_SNAP)

    async def rekey_hello(self, slot_id: int, session_key: bytes | None = None) -> bool:
        """Первым шагом hello, до любого слова сервера: в переключении это
        hello новым ключом — переход фиксируется здесь, и шлюз, закрепляющий
        выбор по первому слову, видит уже записанное решение. ВПС ещё не
        переключился — сессия закрывается молча (False): шлюз придёт снова."""
        res = await asyncio.to_thread(self.services.gateway_rekey_hello, slot_id, session_key)
        await self.notes(res.get("notes"))
        if res.get("rekeyed") and pkg._on_rekeyed is not None:
            try:
                await pkg._on_rekeyed(slot_id)
            except Exception as e:                        # noqa: BLE001
                log.warning("канал линка: после смены ключей слота %s: %s", slot_id, e)
        return not res.get("hold")

    async def rekey_ack_in(self, slot_id: int, msg: dict) -> None:
        action, notes = await asyncio.to_thread(self.services.gateway_rekey_ack_in, slot_id, msg)
        await self.notes(notes)
        if action != "go":
            return
        # сначала link_go в сокет, потом состояние: не ушёл — ничего не меняется,
        # новая сессия повторит link_next с тем же id, шлюз ответит ready
        if not await self.send(slot_id, "link_go", {"id": str(msg.get("id") or "")}):
            log.warning("канал линка: link_go слоту %s не ушёл — переход ждёт новой сессии", slot_id)
            sess = self._sessions.get(slot_id)
            if sess is not None:
                sess.rekey_sent = ""
            return
        await asyncio.to_thread(self.services.gateway_rekey_go, slot_id)
        asyncio.get_running_loop().create_task(self._rekey_commit_later(slot_id))

    async def _rekey_commit_later(self, slot_id: int) -> None:
        """Через пару секунд после link_go — переключение ВПС; вышло — сессию
        слота рвём своей рукой: TCP по линку переживает down/up интерфейса, а
        фиксация ждёт hello новым ключом."""
        await asyncio.sleep(COMMIT_DELAY)
        before = self._sessions.get(slot_id)              # сессия на прежних ключах — её и рвать
        try:
            switched, notes = await asyncio.to_thread(self.services.gateway_rekey_commit, slot_id)
        except Exception as e:                            # noqa: BLE001
            log.warning("канал линка: переключение ключей слота %s: %s", slot_id, e)
            return
        await self.notes(notes)
        if switched and before is not None and self._sessions.get(slot_id) is before:
            # новую сессию (шлюз пришёл новым ключом, пока шёл commit) не трогаем
            await self._drop(slot_id, record=True)

    async def rekey_done_in(self, slot_id: int, msg: dict) -> None:
        await asyncio.to_thread(self.services.gateway_rekey_done_in, slot_id, msg)

    async def rekey_tick(self) -> None:
        """Такт живости: сутки без решения — link_cancel; 180 с без hello новым
        ключом — прежние ключи."""
        await self.rekey_boot()
        try:
            cancel, notes = await asyncio.to_thread(self.services.gateway_rekey_tick)
        except Exception as e:                            # noqa: BLE001
            log.warning("канал линка: такт смены ключей: %s", e)
            return
        for slot_id, rid in cancel:
            if await self.send(slot_id, "link_cancel", {"id": rid}):
                await asyncio.to_thread(self.services.gateway_rekey_cancel_done, slot_id, rid)   # ушёл — из очереди
        for slot_id in list(self._sessions):
            await self.deliver_rekey(slot_id)             # отзывы после возврата — живым сессиям сразу
        await self.notes(notes)
