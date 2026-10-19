"""session.py — жизненный цикл слушателей и сессии слотов: подъём на адресах линков, hello, вытеснение, мёртвые сессии, роль."""
from __future__ import annotations

import asyncio
import time
import contextlib

from awgbot.core import config, settings
from awgbot.util import gwlink
from awgbot.runtime import linkserver as pkg          # порт, адреса и крючки — на пакете: их подменяют тесты

from .common import HELLO_TIMEOUT, _Session, log


class SessionMixin:
    def __init__(self, services):
        self.services = services
        self._loop: asyncio.AbstractEventLoop | None = None   # цикл бота — для touch() из потока
        services.channel_touch = self.touch
        self._servers: dict[tuple[str, int], asyncio.AbstractServer] = {}
        self._sessions: dict[int, _Session] = {}
        self._rejected_at: dict[str, float] = {}     # чужие коннекты: когда последний раз писали в журнал

    def _io(self, slot_id: int, rx: int = 0, tx: int = 0) -> None:
        """Счёт байтов канала по слоту поверх счётчиков линка (services.channel).
        Зонд живости по уликам вычитает их: иначе диагностика, фиды или снимки,
        прошедшие тем же линком, сходили бы за обратный трафик клиентов, и автомат
        считал бы путь наружу живым ровно тогда, когда человек разбирается со
        сломанным слотом."""
        self.services.channel.account(slot_id, rx=rx, tx=tx)

    # ── жизненный цикл ───────────────────────────────────────────────────────

    def _wanted(self) -> set[tuple[str, int]]:
        # прежний порт после смены слушаем ещё неделю: шлюзы переезжают по документу
        cur = pkg.channel_port()
        self.services.gwcfg_channel_port_tick(cur)
        ports = self.services.gwcfg_channel_ports(cur) or [cur]
        out = {(pkg.vps_address(g.link_cidr), port) for g in self.services.db.gateways()
               for port in ports if pkg.vps_address(g.link_cidr)}
        if hasattr(self.services, "gwjoin_addresses"):
            # линки сопряжения: строки слота у нового ещё нет, а канал нужен
            out |= {(addr, port) for _slot, addr in self.services.gwjoin_addresses() for port in ports}
        return out

    async def ensure(self) -> None:
        """Поднять недостающие слушатели и снять лишние — по адресу, не трогая
        остальных. Зовётся со старта и с такта живости: слот могли завести или
        снять, а привязка к адресу существует, только пока поднят линк."""
        want = set(self._wanted())
        for addr in list(self._servers):
            if addr not in want:
                await self._close_server(addr)
        for addr in sorted(want - set(self._servers)):
            try:
                self._servers[addr] = await asyncio.start_server(
                    self._serve, addr[0], addr[1], limit=gwlink.MAX_LINE)
                log.info("канал линка: слушаю %s:%s", *addr)
            except OSError as e:
                # Адрес этого линка не поднят (ребут ВПС, линк не встал) —
                # не ошибка бота, и чужие слоты от неё не страдают.
                log.info("канал линка: слушатель %s:%s не поднят (%s)", addr[0], addr[1], e)

    async def _close_server(self, addr) -> None:
        srv = self._servers.pop(addr, None)
        if srv is None:
            return
        for slot_id in [s for s, sess in self._sessions.items()
                        if (sess.writer.get_extra_info("sockname") or ("",))[0] == addr[0]]:
            await self._drop(slot_id)
        joins = self.__dict__.get("_join_sess") or {}
        for slot_id in [s for s, sess in joins.items()
                        if (sess.writer.get_extra_info("sockname") or ("",))[0] == addr[0]]:
            await self._close_session(joins.pop(slot_id))     # сессия сопряжения — вместе с адресом
        srv.close()
        with contextlib.suppress(Exception):
            await srv.wait_closed()

    async def stop(self) -> None:
        for slot_id in list(self._sessions):
            await self._drop(slot_id)
        for addr in list(self._servers):
            await self._close_server(addr)

    @property
    def _bound(self) -> tuple:
        """Поднятые адреса — для диагностики и тестов."""
        return tuple(sorted(self._servers))

    # ── сессии ───────────────────────────────────────────────────────────────

    def _slot_for(self, local_ip: str, peer_ip: str):
        for gw in self.services.db.gateways():
            if pkg.vps_address(gw.link_cidr) == local_ip and pkg.gw_address(gw.link_cidr) == peer_ip:
                return gw
        return None

    async def _drop(self, slot_id: int, *, record: bool = False) -> None:
        """Снять сессию слота. record — записать закрытие в БД: снятая отсюда
        сессия из своего finally этого не сделает (её уже нет в словаре)."""
        sess = self._sessions.pop(slot_id, None)
        if sess is None:
            return
        await self._close_session(sess)
        if record and (sess.hello or sess.took_over):
            await asyncio.to_thread(self.services.gwlink_session_closed, slot_id)

    @staticmethod
    async def _close_session(sess: "_Session") -> None:
        # close ждёт сдачи буфера: обесточенный шлюз держал бы закрытие до
        # TCP-таймаута — 5 с и abort
        with contextlib.suppress(Exception):
            sess.writer.close()
            try:
                await asyncio.wait_for(sess.writer.wait_closed(), 5)
            except asyncio.TimeoutError:
                sess.writer.transport.abort()

    SEND_TIMEOUT = 30      # с — drain не дождался: сессия мёртвая (шлюз без питания, агент в apt)
    DEAD_LINK_FACTOR = 2   # хендшейк линка старше handshake_max_age × столько — сессии не быть

    async def sweep_dead(self) -> int:
        """Снять сессии слотов, чей линк по данным такта мёртв: без keepalive
        сессия пропавшего шлюза не закрывалась никогда — карточка «на связи»,
        напоминания глушатся, SMB соседа раздаётся дольше суток. Возвращает
        число снятых."""
        if not config.ROUTING_GW_INTERFACE:
            return 0
        from awgbot.infra import routing
        max_age = settings.get_int("app.gateway.handshake_max_age", 300) * self.DEAD_LINK_FACTOR
        dropped = 0
        for slot_id in list(self._sessions):
            gw = await asyncio.to_thread(self.services.db.gateway, slot_id)
            if gw is None:
                continue
            try:
                age = await asyncio.to_thread(routing.link_handshake_age, gw.link_if)
            except Exception:                             # noqa: BLE001
                continue
            if age is not None and age <= max_age:
                continue
            log.info("канал линка: линк слота %s мёртв (хендшейк %s) — снимаю сессию", slot_id,
                     "не было" if age is None else f"{age} с назад")
            await self._drop(slot_id, record=True)
            dropped += 1
        return dropped

    async def send_roles(self) -> None:
        """Такт живости: только роль (сменилась — сказать), без сверки всего."""
        for slot_id in list(self._sessions):
            try:
                await self.send_role(slot_id)
            except Exception as e:                        # noqa: BLE001
                log.warning("канал линка: роль слоту %s: %s", slot_id, e)

    def deliver_soon(self) -> None:
        """Доставка отдельной задачей: такт живости не ждёт drain и сверки."""
        self._loop = asyncio.get_running_loop()
        task = self.__dict__.get("_deliver_task")
        if task is not None and not task.done():
            return
        self.__dict__["_deliver_task"] = self._loop.create_task(self.deliver_all())

    def touch(self) -> None:
        """Правка из чата (сервисы работают в потоке): доставка — сразу, в
        цикле бота, а не на следующей сверке через минуты."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self.deliver_soon)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("", 0)
        local = writer.get_extra_info("sockname") or ("", 0)
        gw = self._slot_for(str(local[0]), str(peer[0]))
        join = (self.services.gwjoin_for_addr(str(local[0]), str(peer[0]))
                if hasattr(self.services, "gwjoin_for_addr") else {})
        if gw is None and join:
            # новый слот в сопряжении: строки нет — линк и ключ по записи сопряжения
            import types
            gw = types.SimpleNamespace(id=int(join["slot"]), link_if=join["link_if"], link_cidr=join["cidr"],
                                       link_port=int(join["port"]), joining=True)
        if gw is None:
            # клиент туннеля в цикле открывает TCP на адрес линка — журнал не заливаем
            now = time.monotonic()
            if now - self._rejected_at.get(str(peer[0]), 0.0) > 600:
                self._rejected_at[str(peer[0])] = now
                log.warning("канал линка: коннект %s → %s не принадлежит ни одному слоту",
                            peer[0], local[0])
            writer.close()
            return
        try:
            key = await asyncio.to_thread(
                lambda: gwlink.channel_key(self.services._link_privkey(gw)))
        except Exception as e:                            # noqa: BLE001
            log.warning("канал линка: ключ слота %s не прочитан: %s", gw.id, e)
            writer.close()
            return
        # Живую сессию слота вытесняет только коннект, прошедший hello с
        # подписью: голый TCP с адреса шлюза (любой процесс на шлюзе, контейнер
        # за маскарадом) раньше рвал настоящую сессию и оставлял «на связи» в БД.
        sess = _Session(writer, key)
        gwlink.tcp_keepalive(writer)
        last_seq: int | None = None
        registered = False
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT)
            except asyncio.TimeoutError:
                log.info("канал линка: слот %s не прислал hello за %s с", gw.id, HELLO_TIMEOUT)
                return
            except ValueError:
                log.warning("канал линка: слот %s прислал строку сверх предела до hello", gw.id)
                return
            if not line:
                return
            self._io(gw.id, rx=len(line))
            try:
                msg = gwlink.unpack(key, line, last_seq=None, nonce=b"")
                if msg.get("t") != "hello":
                    raise gwlink.ProtocolError("сообщение до hello")
                sess.cn = gwlink.nonce_from(msg.get("nonce"))
            except gwlink.ProtocolError as e:
                log.warning("канал линка: слот %s — %s", gw.id, e)
                await asyncio.to_thread(self.services.gwlink_note_error, gw.id, str(e))
                return
            last_seq = int(msg.get("seq") or 0)
            barred = getattr(gw, "joining", False)
            if not barred and hasattr(self.services, "gwjoin_session_barred"):
                # любой слот в сопряжении (замена, «Настроить заново»): ключ
                # канала — из ключей токена, обычная сессия до кода — нет
                barred = await asyncio.to_thread(self.services.gwjoin_session_barred, gw.id)
            if msg.get("join") and join and barred:
                # сессия сопряжения — своя ветка: ни роли, ни доставок (linkserver/join.py).
                # ВПС уже на постоянных ключах (не barred) — hello с join идёт
                # обычной сессией: она и фиксирует смену ключей (join-1)
                await self._serve_join(reader, int(join["slot"]), sess, msg, last_seq)
                return
            if barred:
                log.warning("канал линка: слот %s в сопряжении прислал hello без join — закрываю", gw.id)
                return
            old = self._sessions.get(gw.id)
            sess.took_over = bool(old and (old.hello or old.took_over))
            self._sessions[gw.id] = sess                  # синхронно: без await между проверкой и записью
            registered = True
            if old is not None:
                await self._close_session(old)            # прежняя уходит, её finally «закрытие» не пишет
            if not await self._handle_safe(gw, sess, msg):
                return
            while True:
                try:
                    line = await reader.readline()
                except ValueError:
                    # asyncio бросает это сам, когда строка переросла limit, —
                    # до проверки длины она не доходит вовсе
                    log.warning("канал линка: слот %s прислал строку сверх предела", gw.id)
                    await asyncio.to_thread(self.services.gwlink_note_error, gw.id,
                                            "сообщение длиннее предела")
                    break
                if not line:
                    break
                self._io(gw.id, rx=len(line))
                try:
                    # до hello подпись без нонса (только hello и может пройти),
                    # после — нашим нонсом: чужая сессия не сходится
                    msg = gwlink.unpack(key, line, last_seq=last_seq,
                                        nonce=sess.sn if sess.hello else b"")
                    if not sess.hello:
                        if msg.get("t") != "hello":
                            raise gwlink.ProtocolError("сообщение до hello")
                        sess.cn = gwlink.nonce_from(msg.get("nonce"))
                except gwlink.ProtocolError as e:
                    log.warning("канал линка: слот %s — %s", gw.id, e)
                    await asyncio.to_thread(self.services.gwlink_note_error, gw.id, str(e))
                    break
                last_seq = int(msg.get("seq") or 0)
                if not await self._handle_safe(gw, sess, msg):
                    break
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as e:
            log.info("канал линка: сессия слота %s закрыта (%s)", gw.id, e)
        finally:
            if registered and self._sessions.get(gw.id) is sess:
                self._sessions.pop(gw.id, None)
                # «Последний раз на связи» — только для сессии, которая была:
                # отвергнутый на подписи (чужой ключ, чужая сессия) коннект не освежает
                # отметку, иначе напоминания молчали бы вечно, хотя канал не
                # доставил ни разу
                if sess.hello or sess.took_over:
                    await asyncio.to_thread(self.services.gwlink_session_closed, gw.id)
            with contextlib.suppress(Exception):
                writer.close()
