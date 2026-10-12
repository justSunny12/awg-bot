"""deliver.py — доставки на шлюз по расхождению: настройки, фиды, записи соседей, канон своих списков, роль."""
from __future__ import annotations

import asyncio
import json

from awgbot.core import config
from awgbot.util import gwlink

from .common import SERVER_CAPS, log


class DeliverMixin:
    async def send(self, slot_id: int, kind: str, body: dict | None = None,
                   pad: int = gwlink.PAD_DELTA) -> bool:
        sess = self._sessions.get(slot_id)
        if sess is None or not sess.hello:
            # до hello нонса шлюза нет: подписать нечем, а такт живости или
            # доставка между коннектом и hello подсунули бы клиенту первое
            # слово с пустой подписью — он порвал бы сессию
            return False
        sess.seq += 1
        body = dict(body or {})
        if not sess.sn_sent:
            body["nonce"] = gwlink.nonce_b64(sess.sn)     # наш нонс — первым сообщением
            sess.sn_sent = True
        line = gwlink.pack(sess.key, kind, body, seq=sess.seq, pad=pad, nonce=sess.cn)
        try:
            sess.writer.write(line)
            # drain без предела ждал TCP-сдачи обесточенного шлюза ~15 минут
            # внутри такта живости — автомат переключения стоял
            await asyncio.wait_for(sess.writer.drain(), self.SEND_TIMEOUT)
            self._io(slot_id, tx=len(line))
            return True
        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            why = f"не принял за {self.SEND_TIMEOUT} с" if isinstance(e, asyncio.TimeoutError) else str(e)
            log.info("канал линка: слот %s не принял «%s» (%s)", slot_id, kind, why)
            if self._sessions.get(slot_id) is sess:      # не снять новую сессию вместо этой
                await self._drop(slot_id, record=True)
            else:
                await self._close_session(sess)
            return False

    def online(self, slot_id: int) -> bool:
        return slot_id in self._sessions

    async def deliver(self, slot_id: int) -> bool:
        """Отправить настройки слоту, если снимок расходится с выдаваемым и этот
        набор в сессии ещё не отправляли. Возвращает, ушло ли сообщение."""
        sess = self._sessions.get(slot_id)
        if sess is None or not sess.hello:
            return False
        if "cfg1" in sess.caps:
            return await self.deliver_cfg(slot_id)      # агент с документом: settings ему не нужны
        gw = await asyncio.to_thread(self.services.db.gateway, slot_id)
        if gw is None:
            return False
        want = await asyncio.to_thread(self.services.gwlink_settings_due, gw)
        if not want:
            return False
        digest = gwlink.settings_hash(want)
        if sess.sent_hash == digest:
            return False
        # метка ДО отправки: такт живости и пришедший снимок иначе могли бы
        # отправить один и тот же набор дважды — между проверкой и меткой await
        sess.sent_hash = digest
        sess.sent_queue.append(digest)
        log.info("канал линка: слоту %s уходят настройки", slot_id)
        return await self.send(slot_id, "settings", {"values": want}, pad=gwlink.PAD_SNAP)

    async def deliver_lists(self, slot_id: int) -> bool:
        """Отвезти фиды локальной сети, если у шлюза включён режим без VPN, а
        его отпечаток фидов не совпадает с нашим. Один раз за сессию на набор:
        отвергнутый шлюзом фид не шлём снова до новой сессии или новых фидов."""
        import base64
        import zlib
        sess = self._sessions.get(slot_id)
        if sess is None or not sess.hello:
            return False
        gw = await asyncio.to_thread(self.services.db.gateway, slot_id)
        if gw is None or not gw.lan_mode:
            return False
        digest = await asyncio.to_thread(self.services.gwlink_lan_feeds_digest)
        if not digest or sess.lists_sent == digest:
            return False
        sess.lists_sent = digest                  # метка ДО отправки — см. deliver()
        if sess.lists_have == digest:
            # У шлюза те же фиды. Подтверждаем один раз за сессию — по событию
            # подключения, а не по часам: иначе его запас на своё скачивание
            # протухал бы через 12 часов неизменных фидов.
            return await self.send(slot_id, "lists_ok", {"hash": digest})
        feeds = await asyncio.to_thread(self.services.gwlink_lan_feeds)
        if feeds.get("hash") != digest:
            return False
        packed = zlib.compress(json.dumps({"domains": feeds["domains"], "nets": feeds["nets"]},
                                          ensure_ascii=False).encode(), 9)
        z = base64.b64encode(packed).decode()
        parts = [z[i:i + gwlink.CHUNK] for i in range(0, len(z), gwlink.CHUNK)] or [""]
        if len(parts) > gwlink.MAX_CHUNKS:
            log.warning("канал линка: фиды слишком велики (%s частей) — не шлю, шлюз качает сам",
                        len(parts))
            return False
        log.info("канал линка: слоту %s уходят фиды локальной сети (%s ч.)", slot_id, len(parts))
        if len(parts) == 1:
            return await self.send(slot_id, "lists", {"hash": digest, "z": z})
        for i, part in enumerate(parts):
            if not await self.send(slot_id, "lists_part",
                                   {"hash": digest, "i": i, "n": len(parts), "z": part}):
                return False
        return True

    async def deliver_peer_services(self, slot_id: int) -> bool:
        """Сервисы соседних сетей: отвезти слоту записи соседей, если их
        отпечаток не совпал ни с тем, что шлюз назвал своим, ни с уже ушедшим в
        этой сессии. Пустое к пустому не едет: отпечаток пустого списка — пустая
        строка, а «ещё не слали» — None."""
        sess = self._sessions.get(slot_id)
        if sess is None or not sess.hello:
            return False
        fn = self.services.gwlink_peer_services_for
        gw = await asyncio.to_thread(self.services.db.gateway, slot_id)
        if gw is None:
            return False
        # снимка слота ещё нет (первая сессия, снят слот): что применено на
        # шлюзе — неизвестно, а пустой список следом за полным — два рестарта
        # dnsmasq; снимок придёт сразу после hello и позовёт нас сам
        if not await asyncio.to_thread(self.services.gwlink_snapshot, gw.id):
            return False
        digest, items = await asyncio.to_thread(fn, gw)
        if digest and digest == sess.svc_have:      # у шлюза уже это — считается подтверждением
            await asyncio.to_thread(self.services.gwlink_peer_services_hello_ack, gw.id, digest)
            return False
        if digest == sess.svc_have or digest == sess.svc_sent:
            return False
        sess.svc_sent = digest                     # метка ДО отправки — см. deliver()
        log.info("канал линка: слоту %s уходят сервисы соседей (%s)", slot_id, len(items))
        return await self.send(slot_id, "peer_svc", {"hash": digest, "items": items},
                               pad=gwlink.PAD_SNAP)

    async def deliver_own(self, slot_id: int) -> bool:
        """Канон своих списков — слоту с режимом без VPN и агентом, назвавшим
        own_hash в hello: отпечаток (свой у каждого слота — в нём upto его правок)
        не совпал ни с названным шлюзом, ни с ушедшим в этой сессии."""
        sess = self._sessions.get(slot_id)
        if sess is None or not sess.hello or sess.own_have is None:
            return False
        fn = self.services.gwlink_own_for
        gw = await asyncio.to_thread(self.services.db.gateway, slot_id)
        if gw is None or not gw.lan_mode:
            return False
        digest, body = await asyncio.to_thread(fn, gw)
        if digest == sess.own_have:                 # у шлюза уже этот канон — считается подтверждением
            await asyncio.to_thread(self.services.gwlink_own_hello_ack, gw.id, digest)
            return False
        if digest == sess.own_sent:
            return False
        sess.own_sent = digest
        log.info("канал линка: слоту %s уходят свои списки (%s)", slot_id, len(body.get("items") or []))
        return await self.send(slot_id, "own_set", body, pad=gwlink.PAD_SNAP)

    def _slot_words(self, slot_id: int) -> dict:
        """(standby, name) по базе, без опроса интерфейсов: приветствие не
        должно ждать exec на каждый слот."""
        standby, name = False, ""
        for g in self.services.db.gateways():
            dev = self.services.db.get_device(g.device_id) if g.device_id else None
            if g.id == slot_id:
                if dev is not None:
                    name = dev.name + (f" ({g.label})" if getattr(g, "label", "") else "")
            elif dev is not None:
                standby = True
        return {"standby": standby, "name": name}

    async def deliver_cfg(self, slot_id: int) -> bool:
        """Документ конфигурации слоту с возможностью cfg1: по расхождению
        отпечатков снимка с желаемым, один раз за сессию на отпечаток документа
        (временный отказ агента снимает метку — см. cfg_ack)."""
        sess = self._sessions.get(slot_id)
        if sess is None or not sess.hello or "cfg1" not in sess.caps:
            return False
        gw = await asyncio.to_thread(self.services.db.gateway, slot_id)
        if gw is None:
            return False
        doc = await asyncio.to_thread(self.services.gwcfg_due, gw)
        if not doc:
            return False
        if sess.cfg_sent == doc["hash"]:
            return False
        sess.cfg_sent = doc["hash"]
        log.info("канал линка: слоту %s уходит документ конфигурации №%s", slot_id, doc["gen"])
        return await self.send(slot_id, "cfg", doc, pad=gwlink.PAD_SNAP)

    async def send_role(self, slot_id: int) -> bool:
        """Сказать агенту, активный он или резерв, — только при смене. Сам он
        этого знать не может: решает автомат переключения здесь, на ВПС."""
        sess = self._sessions.get(slot_id)
        if sess is None or not sess.hello:
            return False                      # до hello не отправится — и пометить нельзя
        active = await asyncio.to_thread(self.services.active_gateway)
        is_active = active is not None and active.id == slot_id
        # вместе с ролью — есть ли другой шлюз и как слот назван у сервера:
        # агенту это нужно для честных хвостов и заголовка рецепта роутера
        # и версию сервера: по ней агент выбирает цель обновления (roles.gw.needs_main)
        payload = {"active": is_active, **await asyncio.to_thread(self._slot_words, slot_id),
                   "caps": list(SERVER_CAPS), "server": config.INSTALLED_VERSION}
        if sess.role_sent == payload:
            return False
        sess.role_sent = payload
        return await self.send(slot_id, "role", payload)

    async def deliver_all(self) -> None:
        """Такт живости: у каждой живой сессии проверить, нечего ли доставить.
        Локальная сверка в БД; по сети уходит только то, что разошлось."""
        for slot_id in list(self._sessions):
            try:
                await self.deliver(slot_id)
                await self.deliver_lists(slot_id)
                await self.deliver_peer_services(slot_id)
                await self.deliver_own(slot_id)
                await self.deliver_rekey(slot_id)
                await self.send_role(slot_id)
            except Exception as e:                        # noqa: BLE001
                log.warning("канал линка: доставка слоту %s: %s", slot_id, e)
