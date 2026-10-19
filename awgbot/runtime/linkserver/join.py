"""join.py — сессия сопряжения на стороне слушателя (§3.6–3.7 концепта
«единые обновления»): hello{join,eph} → join_eph, код в чате бота сервера,
join_wrong / join_refused / join_ok, смена ключей `auto=join` по этой же
сессии (link_next, link_ack, link_go), переключение ВПС.

Сессия сопряжения живёт отдельно от сессий слотов: ни `role`, ни документа,
ни списков, ни `keep` — разбирается только `link_ack` (и `link_done`), любое
другое сообщение шлюза — разрыв. После переключения шлюз приходит hello уже
постоянным ключом — обычной сессией слота (фиксация, роль, документ).
"""
from __future__ import annotations

import asyncio
import contextlib

from awgbot.util import gwlink

from awgbot.runtime import linkserver as pkg          # крючки — на пакете
from .common import _Session, log


class JoinMixin:
    @property
    def _join_sessions(self) -> dict:
        return self.__dict__.setdefault("_join_sess", {})

    async def _join_send(self, sess: _Session, kind: str, body: dict | None = None) -> bool:
        sess.seq += 1
        body = dict(body or {})
        if not sess.sn_sent:
            body["nonce"] = gwlink.nonce_b64(sess.sn)
            sess.sn_sent = True
        line = gwlink.pack(sess.key, kind, body, seq=sess.seq, pad=gwlink.PAD_DELTA, nonce=sess.cn)
        try:
            sess.writer.write(line)
            await asyncio.wait_for(sess.writer.drain(), self.SEND_TIMEOUT)
            return True
        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            log.info("канал линка: сопряжение — «%s» не ушло (%s)", kind, e)
            return False

    async def _join_event(self, event: str, st: dict, **extra) -> None:
        """Сообщение подключения в чате бота сервера — крючком main."""
        if pkg._on_join is None:
            return
        try:
            await pkg._on_join(event, st, extra)
        except Exception as e:                            # noqa: BLE001
            log.warning("канал линка: сообщение подключения не поправлено: %s", e)

    async def _serve_join(self, reader, slot: int, sess: _Session, msg: dict, last_seq: int) -> None:
        """Сессия сопряжения после hello с полем join (подпись ключом токена)."""
        res = await asyncio.to_thread(self.services.gwjoin_hello, slot, msg)
        if not res.get("ok"):
            await self._join_send(sess, "join_refused", {"why": res.get("why") or "gone"})
            if res.get("why") == "expired" and res.get("st"):
                err = await self.join_close(slot, "expired")    # отказ ушёл — интерфейс под токен прочь
                if not err:
                    await self._join_event("expired", res["st"])
                elif await asyncio.to_thread(self.services.gwjoin_revert_told, slot, str(res["st"].get("id") or "")):
                    # возврат не удался — сначала честный итог; «срок вышел» скажет такт после возврата
                    await self._join_event("revert_failed", res["st"], err=err)
            return
        old = self._join_sessions.get(slot)
        self._join_sessions[slot] = sess
        sess.join_sid = res["sid"]
        if old is not None and old is not sess:
            await self._close_session(old)            # живая — одна: новая вытесняет прежнюю
        try:
            if not await self._join_send(sess, "join_eph", res["body"]):
                return
            await self._join_event(res["event"], res["st"])
            if res.get("accepted"):
                await self.join_accepted(slot)        # код уже верный — довести с этой сессией
            while True:
                try:
                    line = await reader.readline()
                except ValueError:
                    break
                if not line:
                    break
                try:
                    m = gwlink.unpack(sess.key, line, last_seq=last_seq, nonce=sess.sn)
                except gwlink.ProtocolError as e:
                    log.warning("канал линка: сопряжение слота %s — %s", slot, e)
                    break
                last_seq = int(m.get("seq") or 0)
                kind = m.get("t")
                if kind == "link_ack":
                    await self._join_link_ack(slot, sess, m)
                elif kind == "link_done":
                    continue
                else:
                    log.warning("канал линка: сопряжение слота %s прислало «%s» — разрыв", slot, str(kind)[:32])
                    break
        finally:
            if self._join_sessions.get(slot) is sess:
                self._join_sessions.pop(slot, None)
                await asyncio.to_thread(self.services.gwjoin_session_closed, slot, res["sid"])

    # ── события из чата бота сервера ─────────────────────────────────────────

    async def join_accepted(self, slot: int) -> None:
        """Код верный: секреты (join_ok) и новые ключи (link_next auto=join) —
        живой сессии сопряжения."""
        sess = self._join_sessions.get(slot)
        if sess is None:
            return
        body = await asyncio.to_thread(self.services.gwjoin_ok_body, slot)
        if body:
            await self._join_send(sess, "join_ok", body)
        nxt = await asyncio.to_thread(self.services.gwjoin_next_body, slot)
        if nxt:
            await self._join_send(sess, "link_next", nxt)

    async def join_wrong(self, slot: int, left: int) -> None:
        sess = self._join_sessions.get(slot)
        if sess is not None:
            await self._join_send(sess, "join_wrong", {"left": int(left)})

    async def join_close(self, slot: int, why: str) -> str:
        """Отказ шлюзу (если сессия жива) — затем интерфейс под токен прочь сразу
        (Р24). "" — сделано; иначе причина: запись осталась «closing», такт
        сопряжений повторит возврат."""
        sess = self._join_sessions.pop(slot, None)
        if sess is not None:
            await self._join_send(sess, "join_refused", {"why": why})
            await self._close_session(sess)
        err = await asyncio.to_thread(self.services.gwjoin_revert, slot, why)
        await self.ensure()                           # слушатель на адресе линка — прочь
        return err or ""

    async def _join_link_ack(self, slot: int, sess: _Session, msg: dict) -> None:
        action, notes = await asyncio.to_thread(self.services.gateway_rekey_ack_in, slot, msg)
        await self.notes(notes)
        if action != "go":
            return
        if not await self._join_send(sess, "link_go", {"id": str(msg.get("id") or "")}):
            return
        await asyncio.to_thread(self.services.gateway_rekey_go, slot)
        asyncio.get_running_loop().create_task(self._join_commit_later(slot, sess))

    async def _join_commit_later(self, slot: int, sess: _Session) -> None:
        """Через пару секунд после link_go — переключение ВПС; сессия на ключах
        токена своё отслужила."""
        from .rekey import COMMIT_DELAY
        await asyncio.sleep(COMMIT_DELAY)
        try:
            _switched, notes = await asyncio.to_thread(self.services.gateway_rekey_commit, slot)
        except Exception as e:                        # noqa: BLE001
            log.warning("канал линка: сопряжение слота %s — переключение ключей: %s", slot, e)
            return
        await self.notes(notes)
        if self._join_sessions.get(slot) is sess:
            self._join_sessions.pop(slot, None)
        with contextlib.suppress(Exception):
            await self._close_session(sess)

    async def join_tick(self) -> None:
        """Такт живости: срок вышел — отказ и интерфейс под токен прочь сразу."""
        if not hasattr(self.services, "gwjoin_tick"):
            return
        try:
            closed = await asyncio.to_thread(self.services.gwjoin_tick)
        except Exception as e:                        # noqa: BLE001
            log.warning("канал линка: такт сопряжений: %s", e)
            return
        for st in closed:
            event = st.get("event") or "expired"
            if event == "stale":
                await self._join_event("stale", st)          # запись уже снята — только сказать
                continue
            err = await self.join_close(int(st["slot"]), event)
            if err:
                # возврат не удался — такт повторит; человеку — один раз (quar-4)
                if await asyncio.to_thread(self.services.gwjoin_revert_told, int(st["slot"]), str(st.get("id") or "")):
                    await self._join_event("revert_failed", st, err=err)
                continue
            await self._join_event(event, st)
