"""relay.py — бэкап сервера AWG из чата бота шлюза: части `rst_part` серверу
по каналу и ожидание его ответа `rst_done`. На шлюзе ничего не хранится — блоб
только в памяти на время передачи; обрыв сессии посреди передачи или до ответа —
«связь оборвалась», человек присылает файл снова."""
from __future__ import annotations

import asyncio

from awgbot.domain.backup import relay
from awgbot.util import gwlink

from .common import log

DONE_TIMEOUT = 180            # с: сервер расшифровывает и строит план — секунды, запас на большой бэкап
OFFLINE, NO_CAP, BROKEN = "offline", "no_cap", "broken"
BROKEN_AFTER = "broken_after"   # все части ушли, ответа нет: сервер мог успеть задать вопрос


class RelayMixin:
    _rst_waiting: dict | None = None

    def _rst_futures(self) -> dict:
        if self._rst_waiting is None:
            self._rst_waiting = {}
        return self._rst_waiting

    async def send_backup(self, blob: bytes, name: str) -> dict:
        """Передать бэкап серверу. {"ok", "why": offline|no_cap|broken|refused|"", "error"}."""
        if self._writer is None:
            return {"ok": False, "why": OFFLINE, "error": ""}
        caps = await asyncio.to_thread(self.services.link_server_caps)
        if relay.CAP not in caps:
            return {"ok": False, "why": NO_CAP, "error": ""}
        try:
            parts = await asyncio.to_thread(relay.split, blob, name)   # base64 до 20 МБ — не в цикле событий
        except relay.RelayError as e:
            return {"ok": False, "why": "refused", "error": str(e)}
        tid = parts[0]["id"]
        fut = asyncio.get_running_loop().create_future()
        self._rst_futures()[tid] = fut
        try:
            sent = 0
            for body in parts:
                if fut.done():
                    break                                 # сервер ответил раньше (отказ части) — дальше незачем
                if not await self._send("rst_part", body, pad=gwlink.PAD_DELTA):
                    return {"ok": False, "why": BROKEN, "error": ""}
                sent += 1
            sent_all = sent == len(parts)
            try:
                res = await asyncio.wait_for(asyncio.shield(fut), DONE_TIMEOUT)
            except asyncio.TimeoutError:
                res = None
        finally:
            self._rst_futures().pop(tid, None)
        if res is None:
            # всё ушло, а ответа нет: вопрос мог успеть прийти в чат бота сервера
            return {"ok": False, "why": BROKEN_AFTER if sent_all else BROKEN, "error": ""}
        if res.get("ok"):
            return {"ok": True, "why": "", "error": ""}
        return {"ok": False, "why": "refused", "error": str(res.get("error") or "")[:400]}

    def _rst_done(self, msg: dict) -> None:
        fut = self._rst_futures().get(str(msg.get("id") or ""))
        if fut is not None and not fut.done():
            fut.set_result({"ok": bool(msg.get("ok")), "error": str(msg.get("error") or "")})

    def _rst_broken(self) -> None:
        """Сессия кончилась — ждать ответа не от кого."""
        for fut in self._rst_futures().values():
            if not fut.done():
                fut.set_result(None)
        if self._rst_waiting:
            log.info("канал линка: передача бэкапа серверу оборвалась вместе с сессией")
