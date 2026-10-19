"""inbound.py — разбор входящих: снимок и дельты, ответы на доставки, свои списки, сервисы, installed, claim;
набор обновлений: hello и `upd_ack` — в upd.py."""
from __future__ import annotations

import asyncio
import contextlib

from awgbot.domain import gwownlists, gwservices, gwsnapshot
from awgbot.util import gwlink
from awgbot.runtime import linkserver as pkg          # порт, адреса и крючки — на пакете: их подменяют тесты

from .common import _Session, log


class InboundMixin:
    # ── разбор сообщений ─────────────────────────────────────────────────────

    async def _handle_safe(self, gw, sess: _Session, msg: dict) -> bool:
        """Исключение обработчика (после проверки подписи — только от стороны с
        ключом) — в журнал и отказ сессии, а не падение задачи слушателя."""
        try:
            await self._handle(gw, sess, msg)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:                            # noqa: BLE001
            kind = repr(str(msg.get("t"))[:32])
            log.warning("канал линка: слот %s — сообщение %s не обработано: %s", gw.id, kind, e)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.services.gwlink_note_error, gw.id,
                                        f"сообщение {kind} не обработано")
            return False

    async def _handle(self, gw, sess: _Session, msg: dict) -> None:
        kind = msg.get("t")
        if kind == "hello":
            proto = int(msg.get("proto") or 0)
            sess.hello = True
            raw_caps = msg.get("caps")
            sess.caps = ["".join(ch for ch in c if ch.isalnum())[:16]
                         for c in (raw_caps if isinstance(raw_caps, list) else [])[:16]
                         if isinstance(c, str) and c]
            await asyncio.to_thread(self.services.gwlink_session_opened, gw.id,
                                    str(msg.get("agent") or "")[:32], proto, sess.caps)
            sess.lists_have = str(msg.get("lists_hash") or "")[:64]
            sess.svc_have = str(msg.get("svc_hash") or "")[:64]
            sess.own_have = str(msg.get("own_hash") or "")[:64] if "own_hash" in msg else None
            await asyncio.to_thread(self.services.gwlink_own_hello_in, gw.id, sess.own_have is not None)
            # смена ключей — до первого слова сервера: hello новым ключом
            # фиксирует переход, а шлюз закрепляет выбор по первому слову
            if not await self.rekey_hello(gw.id, sess.key):
                log.info("канал линка: слот %s пришёл посреди переключения ключей — закрываю, придёт снова", gw.id)
                await self._drop(gw.id, record=True)
                return
            # роль — первым сообщением сервера: по нему клиент понимает, что
            # сессия настоящая, и только тогда сбрасывает свой бэкофф
            await self.send_role(gw.id)
            if hasattr(self.services, "restore_wait_hello"):
                # после восстановления ждали именно этого факта
                await self.notes(await asyncio.to_thread(self.services.restore_wait_hello, gw.id))
            if "keep1" in sess.caps:
                # копия настроек шлюза — до документа: своя почта шлюза ложится
                # раньше раздела mail, и тот видит памятку
                reply, body = await asyncio.to_thread(
                    self.services.gwkeep_hello, gw.id, str(msg.get("keep_hash") or ""),
                    bool(msg.get("keep_new")), bool(msg.get("keep_fresh")))
                if reply == "keep_back":
                    await self.send(gw.id, "keep_back", body)
                elif reply == "keep_none":
                    await self.send(gw.id, "keep_none", body)
                else:
                    await self.send(gw.id, "keep_ok", body)
            await self.deliver_lists(gw.id)
            await self.deliver_peer_services(gw.id)
            await self.deliver_own(gw.id)
            await self.deliver_rekey(gw.id)
            # набор обновлений: удержание прошлого хода, догон, очередь — после
            # доставок: роль и документ шлюзу важнее
            from . import upd as _upd
            await _upd.on_hello(self.services, gw.id, self)
            if proto != gwlink.PROTO:
                log.info("канал линка: слот %s говорит proto=%s, у нас %s",
                         gw.id, proto, gwlink.PROTO)
            return
        if not sess.hello:
            return                                        # не бывает: отсеяно при разборе
        if kind in ("snap", "delta"):
            patch = gwsnapshot.sanitize(msg)
            rev = int(msg.get("rev") or 0)
            ok = await asyncio.to_thread(self.services.gwlink_snapshot_in, gw.id, patch,
                                         rev, kind == "snap")
            if ok:
                if kind == "snap" or "cfg" in msg:
                    # отпечатки стоящего изменились (в том числе файлом, применённым
                    # руками): документ с тем же отпечатком можно слать снова
                    sess.cfg_sent = ""
                # снимок показал, что стоит на шлюзе, — самое время доставить
                await self.deliver(gw.id)
                # подсети соседей применены — теперь есть куда вести записи
                await self.deliver_peer_services(gw.id)
            else:
                # Разрыв нумерации: дельта потерялась или пришла не по порядку.
                # Просим полный снимок и начинаем счёт заново.
                await self.send(gw.id, "ask", {"what": "snap"})
            return
        if kind == "lists_ack":
            if msg.get("ok"):
                sess.lists_have = str(msg.get("hash") or "")[:64]
            await asyncio.to_thread(self.services.gwlink_lists_ack_in, gw.id, msg)
            return
        if kind == "link_ack":
            await self.rekey_ack_in(gw.id, msg)
            return
        if kind == "link_done":
            await self.rekey_done_in(gw.id, msg)
            return
        if kind == "cfg_ack":
            ack, notes = await asyncio.to_thread(self.services.gwcfg_ack_take, gw.id, msg)
            await self.notes(notes)                       # Н5: аплинк отклонили в чате шлюза
            if ack.get("retry"):
                # временный отказ (юнит обвязки ещё работал): тот же документ
                # уйдёт снова по следующему расхождению снимка
                sess.cfg_sent = ""
            if "env" in ack.get("applied", []):
                # режим без VPN мог включиться документом: фиды, отправленные при
                # hello, шлюз отверг («режим выключен») — теперь есть кому принять
                sess.lists_sent = ""
                await self.deliver_lists(gw.id)
                await self.deliver_peer_services(gw.id)
                sess.own_sent = None
                await self.deliver_own(gw.id)
            return
        if kind == "upd_ack":
            from . import upd as _upd
            await _upd.on_ack(self.services, gw.id, msg, self)
            return
        if kind == "rst_part":
            await self._rst_part(gw, sess, msg)
            return
        if kind == "keep":
            digest = await asyncio.to_thread(self.services.gwkeep_in, gw.id, msg)
            await self.send(gw.id, "keep_ok", {"hash": digest})
            return
        if kind == "claim":
            token = str(msg.get("token") or "")[:4096]
            await asyncio.to_thread(self.services.gwlink_claim_in, gw.id, token)
            return
        if kind == "svc":
            # сервисы сети этого слота:
            # изменились — соседям на связи уходит новый список
            items = msg.get("items") if isinstance(msg.get("items"), list) else []
            changed = await asyncio.to_thread(self.services.gwlink_services_in, gw.id,
                                              items[:gwservices.MAX_OWN * 2])
            if changed:
                for other in list(self._sessions):
                    if other != gw.id:
                        await self.deliver_peer_services(other)
                # «SMB в туннеле»: записи резолвера админа — тем же списком
                await asyncio.to_thread(self.services.smb_tunnel_sync)
            # прошлые записи этот слот не принял (поломка на шлюзе) — его
            # список приходит и как просьба повторить их
            ack = await asyncio.to_thread(self.services.gwlink_peer_services_ack, gw.id)
            if ack and not ack.get("ok"):
                sess.svc_sent = None
                await self.deliver_peer_services(gw.id)
            return
        if kind == "own_ev":
            # правки своих списков с этого шлюза: слить в канон и раздать —
            # ему сразу (ответ с upto), остальным на связи тоже сразу
            events = msg.get("ev") if isinstance(msg.get("ev"), list) else []
            changed = await asyncio.to_thread(self.services.gwlink_own_in, gw.id,
                                              str(msg.get("run") or "")[:32], events[:gwownlists.MAX_EVENTS])
            sess.own_sent = None
            await self.deliver_own(gw.id)
            if changed:
                for other in list(self._sessions):
                    if other != gw.id:
                        await self.deliver_own(other)
            return
        if kind == "own_ack":
            if msg.get("ok"):
                sess.own_have = str(msg.get("hash") or "")[:64]
            await asyncio.to_thread(self.services.gwlink_own_ack_in, gw.id, msg)
            return
        if kind == "peer_svc_ack":
            if msg.get("ok"):
                sess.svc_have = str(msg.get("hash") or "")[:64]
            await asyncio.to_thread(self.services.gwlink_peer_services_ack_in, gw.id, msg)
            return
        if kind == "join_done":
            # конец сопряжения своим словом агента (полный агент, файл
            # сопряжения ещё на диске): итог Т-П9 — и у агента, стоявшего
            # раньше, у которого «установлен» не уходит; ответ — join_done_ok
            st = await asyncio.to_thread(self.services.gwjoin_installed, gw.id,
                                         str(msg.get("id") or "")[:32])
            await self.send(gw.id, "join_done_ok", {"id": str(msg.get("id") or "")[:32]})
            if st:
                await self._join_event("done", st)
            return
        if kind == "installed":
            # агент нового шлюза первый раз на связи: установщик отработал —
            # админу «✅ … успешно настроен» (крючок main). Снимок к этому
            # моменту уже принят: агент шлёт installed после него. Шлюз,
            # подключённый командой, — итог сообщением подключения (Т-П9)
            if hasattr(self.services, "gwjoin_installed"):
                st = await asyncio.to_thread(self.services.gwjoin_installed, gw.id)
                if st:
                    await self._join_event("done", st)
                    return
            if pkg._on_installed is not None:
                try:
                    await pkg._on_installed(gw.id)
                except Exception as e:                    # noqa: BLE001
                    log.warning("канал линка: уведомление о настройке слота %s не показано: %s", gw.id, e)
            return
        log.info("канал линка: слот %s прислал неизвестное %s — игнорирую", gw.id, repr(str(kind)[:32]))
