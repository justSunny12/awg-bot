"""relay.py — бэкап сервера AWG, присланный в чат бота шлюза: части `rst_part`
по каналу, сборка с контрольной суммой и пределом (domain/backup/relay), затем
штатные рельсы восстановления из чата — крючок main (осмотр, вопрос в чате бота
сервера). Ответ шлюзу — `rst_done`: принят или отказ словами. При отказе в чат
бота сервера не идёт ничего: человек смотрит в чат бота шлюза. Обрыв сессии
стирает собранное (оно живёт в сессии).

Осмотр — расшифровка ключевой фразой (argon2, сотни мегабайт памяти): один на
весь сервер за раз, по слоту — не чаще раза в RATE_S, после отказа — пауза
PAUSE_S; блоб короче настоящего бэкапа отвергается до осмотра. Новая передача
вытесняет недособранную — по её id сразу отказ, чтобы агент не ждал зря."""
from __future__ import annotations

import asyncio
import time

from awgbot.domain.backup import relay
from awgbot.runtime import linkserver as pkg          # крючок восстановления — на пакете: его ставит main

from .common import _Session, log

RATE_S = 60                   # с: осмотр через шлюз одного слота — не чаще
PAUSE_S = 300                 # с: после отказа осмотра — пауза слоту
_inspect_lock = asyncio.Lock()   # один осмотр через шлюз на весь сервер


class RelayMixin:
    _rst_next: dict | None = None    # слот → monotonic, раньше которого осмотр слоту не положен

    def _rst_gate(self, slot_id: int) -> str:
        """«» — осмотр можно; иначе причина для шлюза."""
        nxt = (self._rst_next or {}).get(slot_id, 0.0)
        left = int(nxt - time.monotonic())
        if left > 0:
            return f"файл бэкапа с этого шлюза уже приходил недавно — повтори через {max(left, 1)} с"
        return ""

    def _rst_mark(self, slot_id: int, refused: bool) -> None:
        if self._rst_next is None:
            self._rst_next = {}
        self._rst_next[slot_id] = time.monotonic() + (PAUSE_S if refused else RATE_S)

    async def _rst_part(self, gw, sess: _Session, msg: dict) -> None:
        tid = str(msg.get("id") or "")[:16]
        asm = getattr(sess, "rst", None)
        try:
            if asm is None or not asm.same(msg):
                if asm is not None and not asm.done:
                    # прежнюю вытеснили: агент ждёт её ответа — отказ сразу, а не по таймауту
                    await self.send(gw.id, "rst_done", {"id": asm.id, "ok": False,
                                                        "error": "передачу вытеснил новый бэкап"})
                asm = sess.rst = relay.Assembly(msg)
            asm.add(msg)
            if not asm.done:
                return
            blob = await asyncio.to_thread(asm.blob)     # sha256 до 20 МБ — не в цикле событий
        except relay.RelayError as e:
            sess.rst = None
            await self.send(gw.id, "rst_done", {"id": tid, "ok": False, "error": str(e)})
            return
        name, size = asm.name, asm.size
        asm.parts.clear()                                 # собранное — только в blob; сессия его не держит
        sess.rst = None
        error = ""
        if len(blob) < relay.MIN_BYTES:
            error = "файл не похож на бэкап сервера AWG (слишком мал)"
        elif pkg._on_restore is None:
            error = "сервер AWG сейчас не принимает бэкапы через шлюз"
        else:
            error = self._rst_gate(gw.id)
        if not error:
            async with _inspect_lock:
                try:
                    error = await pkg._on_restore(gw.id, blob, name) or ""
                except Exception as e:                    # noqa: BLE001
                    log.warning("канал линка: слот %s — бэкап через шлюз не осмотрен: %s", gw.id, e)
                    error = "сервер AWG не смог разобрать бэкап"
            self._rst_mark(gw.id, refused=bool(error))
        del blob
        log.info("канал линка: слот %s прислал бэкап сервера (%s байт) — %s", gw.id, size,
                 "вопрос в чате бота сервера" if not error else error)
        await self.send(gw.id, "rst_done", {"id": tid, "ok": not error, "error": error[:400]})
