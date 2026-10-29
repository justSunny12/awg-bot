"""dispatch.py — разбор входящих от ВПС."""
from __future__ import annotations

import asyncio

from .common import log


class DispatchMixin:
    async def _handle(self, msg: dict) -> None:
        """Разбор одного сообщения. Исключение внутри — в журнал, а не наружу:
        иначе поломка одного обработчика рвала бы сессию целиком, и та же
        доставка повторялась бы на каждом переподключении."""
        try:
            await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:                            # noqa: BLE001
            log.warning("канал линка: сообщение «%s» не обработано: %s", msg.get("t"), e)

    async def _dispatch(self, msg: dict) -> None:
        # любое слово сервера — жизнь основы для взгляда шлюза (резерв)
        if hasattr(self.services, "reserve_gw_word"):
            self.services.reserve_gw_word()
        if msg.get("t") == "pong":
            if hasattr(self.services, "reserve_gw_pong_in"):
                self.services.reserve_gw_pong_in(str(msg.get("n") or ""))
            return
        if msg.get("t") == "lists_part":
            whole = self._collect_part(msg)
            if whole is not None:
                await self._apply_lists(str(msg.get("hash") or "")[:64], whole)
            return
        if msg.get("t") == "ask" and msg.get("what") == "snap":
            await self.push(full=True)
            return
        if msg.get("t") == "role":
            await asyncio.to_thread(self.services.set_link_role, bool(msg.get("active")),
                                    msg.get("standby"), msg.get("name"),
                                    # сервер 3.3.0 шлёт caps в каждом role; нет поля — сервер прежний,
                                    # возможностей нет (откат или копия): сохранённое стирается
                                    msg.get("caps") if isinstance(msg.get("caps"), list) else [],
                                    # и версию: нет поля — сервер старше 3.3.0
                                    msg.get("server"),
                                    # адреса в туннеле своей сети — для показа; форму проверяет домен
                                    msg.get("lan_virtual"),
                                    # бот сервера — ссылкой на его чат (с 3.4.0; нет поля — пусто)
                                    msg.get("bot"))
            return
        if msg.get("t") == "lists_ok":
            # сервер при подключении подтвердил: у нас те же фиды, что у него
            await asyncio.to_thread(self.services.lan_feeds_touch, str(msg.get("hash") or ""))
            return
        if msg.get("t") == "lists":
            await self._apply_lists(str(msg.get("hash") or "")[:64], str(msg.get("z") or ""))
            return
        if msg.get("t") == "peer_svc":
            await self._apply_peer_services(msg)
            return
        if msg.get("t") == "own_set":
            await self._apply_own(msg)
            return
        if msg.get("t") == "link_next":
            await self._rekey_next(msg)
            return
        if msg.get("t") == "link_go":
            await self._rekey_go(msg)
            return
        if msg.get("t") == "link_cancel":
            await self._rekey_cancel(msg)
            return
        if msg.get("t") == "rst_done":
            self._rst_done(msg)
            return
        if msg.get("t") in ("keep_ok", "keep_none", "keep_back"):
            await self._keep_reply(msg)
            return
        if msg.get("t") == "upd":
            await self._upd_in(msg)
            return
        if msg.get("t") == "upd_commit":
            await self._upd_commit(msg)
            return
        if msg.get("t") == "upd_revert":
            await self._upd_revert(msg)
            return
        if msg.get("t") == "cfg":
            await self._apply_cfg(msg)
            return
        if msg.get("t") in ("qask", "qtake", "qrel", "qneed", "qpause", "qstate"):
            # кворум: свой сервер — кандидат в отъезде своей линии (те же слова, что у резерва)
            if hasattr(self.services, "quorum_guest_in"):
                from awgbot.runtime import guestlink
                actions = await asyncio.to_thread(self.services.quorum_guest_in, str(msg.get("t")), msg)
                await guestlink.run_actions(self.services, actions)
            return
        if msg.get("t") == "join_done_ok":
            if hasattr(self.services, "join_done_sent"):
                await asyncio.to_thread(self.services.join_done_sent)
            return
        if msg.get("t") in ("join_eph", "join_wrong", "join_refused", "join_ok"):
            if hasattr(self.services, "join_state"):
                await self._join_in(msg)
            return
        log.info("канал линка: с ВПС пришло неизвестное «%s» — игнорирую", msg.get("t"))
