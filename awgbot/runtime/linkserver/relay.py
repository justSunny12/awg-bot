"""relay.py — бэкап сервера AWG, присланный в чат бота шлюза: части `rst_part`
по каналу, сборка с контрольной суммой и пределом (domain/backup/relay), затем
штатные рельсы восстановления из чата — крючок main (осмотр, вопрос в чате бота
сервера). Ответ шлюзу — `rst_done`: принят или отказ словами. При отказе в чат
бота сервера не идёт ничего: человек смотрит в чат бота шлюза. Обрыв сессии
стирает собранное (оно живёт в сессии)."""
from __future__ import annotations

from awgbot.domain.backup import relay
from awgbot.runtime import linkserver as pkg          # крючок восстановления — на пакете: его ставит main

from .common import _Session, log


class RelayMixin:
    async def _rst_part(self, gw, sess: _Session, msg: dict) -> None:
        tid = str(msg.get("id") or "")[:16]
        asm = getattr(sess, "rst", None)
        try:
            if asm is None or not asm.same(msg):
                asm = sess.rst = relay.Assembly(msg)          # новая передача — прежняя сборка прочь
            asm.add(msg)
            if not asm.done:
                return
            blob = asm.blob()
        except relay.RelayError as e:
            sess.rst = None
            await self.send(gw.id, "rst_done", {"id": tid, "ok": False, "error": str(e)})
            return
        sess.rst = None
        error = "сервер AWG сейчас не принимает бэкапы через шлюз" if pkg._on_restore is None else ""
        if not error:
            try:
                error = await pkg._on_restore(gw.id, blob, asm.name) or ""
            except Exception as e:                        # noqa: BLE001
                log.warning("канал линка: слот %s — бэкап через шлюз не осмотрен: %s", gw.id, e)
                error = "сервер AWG не смог разобрать бэкап"
        log.info("канал линка: слот %s прислал бэкап сервера (%s байт) — %s", gw.id, asm.size,
                 "вопрос в чате бота сервера" if not error else error)
        await self.send(gw.id, "rst_done", {"id": tid, "ok": not error, "error": error[:400]})
