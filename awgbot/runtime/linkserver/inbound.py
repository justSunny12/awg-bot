"""inbound.py — разбор входящих: снимок и дельты, ответы на доставки, свои списки, сервисы, installed, claim, итог файла;
пересчёт цели обновления по версиям агентов."""
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

    async def _update_regate(self) -> None:
        """Версии шлюзов могли смениться — цель обновления сервера
        пересчитывается по сохранённому списку релизов, без сети; новая
        допустимая — уведомление крючком main (один раз на тег)."""
        regate = getattr(self.services, "update_regate", None)
        if regate is None:
            return
        nxt = await asyncio.to_thread(regate)
        if nxt is not None and pkg._on_update is not None:
            try:
                await pkg._on_update(nxt)
            except Exception as e:                        # noqa: BLE001
                log.warning("канал линка: уведомление об обновлении не ушло: %s", e)

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
            await self.deliver_lists(gw.id)
            await self.deliver_peer_services(gw.id)
            await self.deliver_own(gw.id)
            await self.deliver_rekey(gw.id)
            # версия агента и свежесть связи — вход условия обновления сервера;
            # после доставок: уведомление в Telegram не задерживает роль шлюзу
            await self._update_regate()
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
                    # отпечатки стоящего изменились (в том числе старым файлом, применённым
                    # руками): документ с тем же отпечатком можно слать снова
                    sess.cfg_sent = ""
                # снимок показал, что стоит на шлюзе, — самое время доставить
                await self.deliver(gw.id)
                # подсети соседей применены — теперь есть куда вести записи
                await self.deliver_peer_services(gw.id)
                if kind == "snap" or "agent_version" in patch:
                    await self._update_regate()           # версия агента — вход условия обновления
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
                # режим без VPN мог включиться документом: фиды и записи соседей —
                # заново, как после ack с LAN_MODE
                sess.lists_sent = ""
                await self.deliver_lists(gw.id)
                await self.deliver_peer_services(gw.id)
                sess.own_sent = None
                await self.deliver_own(gw.id)
            return
        if kind == "ack":
            # отпечаток набора, на который это ответ, — свой (первый из ушедших в
            # сессии и ещё не отвеченных: агент отвечает по одному на каждый, по
            # порядку), не из сообщения: по нему отказ на текущее отличается от
            # отказа на прежнее. Временный отказ (retry) — не отказ: без отпечатка
            answered = sess.sent_queue.pop(0) if sess.sent_queue else sess.sent_hash
            retry = bool(not msg.get("ok") and msg.get("retry"))
            await asyncio.to_thread(self.services.gwlink_ack_in, gw.id,
                                    {**msg, "hash": "" if retry else answered})
            if retry and answered == sess.sent_hash:
                # временный отказ агента (юнит обвязки ещё работал): тот же
                # набор можно слать снова — по расхождению снимка, как обычно
                sess.sent_hash = ""
            if msg.get("ok") and "LAN_MODE" in (msg.get("changed") or []):
                # Режим без VPN только что включился каналом: фиды, отправленные
                # при hello, шлюз отверг («режим выключен»), а повторно за сессию
                # они не уходят. Теперь есть кому их принять — шлём заново, иначе
                # локальная сеть ждала бы своих фидов до следующего скачивания агента.
                sess.lists_sent = ""
                await self.deliver_lists(gw.id)
                sess.own_sent = None
                await self.deliver_own(gw.id)
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
        if kind == "installed":
            # агент нового шлюза первый раз на связи: установщик отработал —
            # админу «✅ … успешно настроен» (крючок main). Снимок к этому
            # моменту уже принят: агент шлёт installed после него.
            if pkg._on_installed is not None:
                try:
                    await pkg._on_installed(gw.id)
                except Exception as e:                    # noqa: BLE001
                    log.warning("канал линка: уведомление о настройке слота %s не показано: %s", gw.id, e)
            return
        if kind == "applied":
            # шлюз применил файл конфигурации из своего чата (или не смог) —
            # сразу, не дожидаясь снимка: человек ждёт итог у файла на сервере
            res = await asyncio.to_thread(self.services.gwlink_applied_in, gw.id, msg)
            # подтверждение — первым: агент держит итог в очереди до него, и
            # повтор того же итога (сессия умерла с буфером) сервер узнаёт сам
            await self.send(gw.id, "applied_ack", {"fp": str(res.get("fp") or ""),
                                                   "at": str(res.get("at") or "")})
            if pkg._on_applied is not None and not res.get("dup"):
                try:
                    await pkg._on_applied(gw.id, bool(res.get("ok")), str(res.get("error") or ""),
                                      str(res.get("fp") or ""))
                except Exception as e:                    # noqa: BLE001
                    log.warning("канал линка: итог применения слота %s не показан: %s", gw.id, e)
            return
        log.info("канал линка: слот %s прислал неизвестное %s — игнорирую", gw.id, repr(str(kind)[:32]))
