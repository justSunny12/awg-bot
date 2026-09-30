"""
linkserver.py — сторона ВПС для канала ВПС ↔ шлюз.

Слушатель живёт ТОЛЬКО на адресах линков: публичный адрес сервера порт канала
не показывает даже при снятом файерволе. Коннект всегда устанавливает шлюз —
на малине не появляется ни одного слушающего сокета, и механика одинаково
работает за любым NAT.

По серверу на адрес линка, а не один на все: bind списка адресов в asyncio —
«всё или ничего», и лежащий второй линк оставлял бы без канала здоровый
первый. Отдельные серверы изолируют отказ по слотам и не рвут чужие сессии,
когда состав слотов меняется.

Слот определяется парой «на какой локальный адрес пришли» и «каким ключом линка
проверилась подпись»: обе должны указывать на один слот, иначе разрыв. Вторая
сессия того же слота вытесняет первую — малина перезагрузилась, старая висит.
Повтор отсекают нонсы сессии (gwlink, proto 2): hello приносит нонс шлюза
(поле `nonce`), первое сообщение сервера несёт его нонс тем же полем, дальше
каждая сторона подписывает нонсом получателя. Часы в проверку не входят. До
hello сервер не шлёт ничего.
Ключ читается заново на каждую сессию: после замены машины в слоте ключ линка
новый, а номер слота тот же, и кеш по номеру держал бы канал мёртвым до
перезапуска бота.

Периодического прикладного обмена здесь нет вовсе: сессия держится открытой,
в простое прикладных сообщений ноль. TCP-keepalive ядра на сокете включён
(`gwlink.tcp_keepalive`: 20 мин / 60 с / 3 пробы) — он идёт внутри линка и
снаружи не виден, а полуоткрытую сессию добивает за минуты. Мёртвые сессии
снимаются и раньше: такт живости (`liveness_tick`) снимает сессию слота, чей
хендшейк линка старше двойного `gateway.handshake_max_age` (`sweep_dead`),
отправка, не сданная за `SEND_TIMEOUT`, снимает сессию, закрытие не ждёт
сдачи буфера дольше 5 с. Исключение обработчика — отказ сессии с причиной в
state слота (`_handle_safe`), а не падение задачи слушателя.

Доставка на шлюз — по расхождению, а не по расписанию: на каждом снимке, при
старте (`ensure`), каждый четвёртый такт живости и на тике монитора
(`monitor_tick`) — отдельной задачей `deliver_soon`, такт её не ждёт — сверяется
локально, что разошлось,
и по сети уходит только это — настройки (`deliver`), фиды локальной сети
(`deliver_lists`), записи SMB соседних сетей (`deliver_peer_services`), канон
своих списков (`deliver_own`), роль слота (`send_role`: активен ли, есть ли
другой слот с устройством, имя слота; её такт говорит каждый раз). Один и тот же набор
за сессию повторно не уходит. Полный снимок сервер просит сам (`ask snap`) —
только при разрыве нумерации дельт; по запросу человека ничего у шлюза не
спрашивается, диагностики по каналу нет.

То, что человек сделал руками в чате агента, до остальных шлюзов доходит
сразу, а не в такте: список SMB (`svc`) и правки своих списков (`own_ev`)
после разбора тут же раздаются всем сессиям на связи; такт живости остаётся страховкой.

Байты канала по слоту копятся в `services.channel`: зонд живости по уликам
линка вычитает их, чтобы снимки и фиды не сходили за обратный трафик клиентов.
"""
from __future__ import annotations

import asyncio
import time
import contextlib
import json
import logging

from awgbot.core import config, settings
from awgbot.domain import gwownlists, gwservices, gwsnapshot
from awgbot.util import gwlink

HELLO_TIMEOUT = 15       # с: первое слово шлюза, иначе коннект отбрасывается без вытеснения живой сессии

log = logging.getLogger(__name__)

DEFAULT_PORT = gwlink.DEFAULT_PORT


def channel_port() -> int:
    """Порт слушателя — из общего места (gwlink); своё имя оставлено, чтобы
    тесты подменяли порт слушателю, не трогая бандл и сторож."""
    return gwlink.channel_port()


def vps_address(link_cidr: str) -> str:
    """Адрес ВПС в /30 линка — первый хост, как его считает скрипт линка."""
    return gwlink.link_hosts(link_cidr)[0]


def gw_address(link_cidr: str) -> str:
    """Адрес шлюза в /30 линка — второй хост."""
    return gwlink.link_hosts(link_cidr)[1]


class _Session:
    """Всё, что живёт ровно одну сессию слота. Одно место сброса вместо шести
    параллельных словарей: новое «уже отправили в этой сессии» иначе однажды
    забыли бы сбросить, и оно пережило бы переподключение шлюза."""

    def __init__(self, writer: asyncio.StreamWriter, key: bytes):
        self.writer = writer
        self.key = key
        self.seq = 0
        self.hello = False               # был ли принят hello — только тогда сессия «была»
        self.sent_hash = ""              # набор настроек, уже отправленный в этой сессии
        self.lists_have = ""             # отпечаток фидов, который шлюз назвал своим
        self.lists_sent = ""             # отпечаток фидов, уже ушедший в этой сессии
        self.svc_have = ""               # отпечаток записей соседей, который шлюз назвал своим
        self.svc_sent: str | None = None # отпечаток записей соседей, ушедший в этой сессии
        self.own_have: str | None = None # отпечаток канона своих списков у шлюза; None — агент синхронизацию не знает
        self.own_sent: str | None = None # отпечаток канона, ушедший в этой сессии
        self.role_sent: dict | None = None
        # вытеснила сессию, прошедшую hello: запись «на связи» в БД — её, и
        # закрыть её обязана эта, даже если сама до hello не дойдёт
        self.took_over = False
        self.sn = gwlink.new_nonce()     # наш нонс: им шлюз подписывает всё после hello
        self.cn = b""                    # нонс шлюза из hello: им подписываем мы
        self.sn_sent = False             # первое наше сообщение несёт sn


class LinkServer:
    """По серверу на адрес линка, по сессии на слот."""

    def __init__(self, services):
        self.services = services
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
        port = channel_port()
        return {(vps_address(g.link_cidr), port) for g in self.services.db.gateways()
                if vps_address(g.link_cidr)}

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
            if vps_address(gw.link_cidr) == local_ip and gw_address(gw.link_cidr) == peer_ip:
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
        # close ждёт сдачи буфера: обесточенная малина держала бы закрытие до
        # TCP-таймаута — 5 с и abort
        with contextlib.suppress(Exception):
            sess.writer.close()
            try:
                await asyncio.wait_for(sess.writer.wait_closed(), 5)
            except asyncio.TimeoutError:
                sess.writer.transport.abort()

    SEND_TIMEOUT = 30      # с — drain не дождался: сессия мёртвая (малина без питания, агент в apt)
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
        task = self.__dict__.get("_deliver_task")
        if task is not None and not task.done():
            return
        self.__dict__["_deliver_task"] = asyncio.get_running_loop().create_task(self.deliver_all())

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("", 0)
        local = writer.get_extra_info("sockname") or ("", 0)
        gw = self._slot_for(str(local[0]), str(peer[0]))
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
        # подписью: голый TCP с адреса шлюза (любой процесс на малине, контейнер
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
            await asyncio.to_thread(self.services.gwlink_session_opened, gw.id,
                                    str(msg.get("agent") or "")[:32], proto)
            sess.lists_have = str(msg.get("lists_hash") or "")[:64]
            sess.svc_have = str(msg.get("svc_hash") or "")[:64]
            sess.own_have = str(msg.get("own_hash") or "")[:64] if "own_hash" in msg else None
            await asyncio.to_thread(self.services.gwlink_own_hello_in, gw.id, sess.own_have is not None)
            # роль — первым сообщением сервера: по нему клиент понимает, что
            # сессия настоящая, и только тогда сбрасывает свой бэкофф
            await self.send_role(gw.id)
            await self.deliver_lists(gw.id)
            await self.deliver_peer_services(gw.id)
            await self.deliver_own(gw.id)
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
        if kind == "ack":
            await asyncio.to_thread(self.services.gwlink_ack_in, gw.id, msg)
            if not msg.get("ok") and msg.get("retry"):
                # временный отказ агента (юнит обвязки ещё работал): тот же
                # набор можно слать снова — по расхождению снимка, как обычно
                sess.sent_hash = ""
            if msg.get("ok") and "LAN_MODE" in (msg.get("changed") or []):
                # Режим без VPN только что включился каналом: фиды, отправленные
                # при hello, шлюз отверг («режим выключен»), а повторно за сессию
                # они не уходят. Теперь есть кому их принять — шлём заново, иначе
                # квартира ждала бы своих фидов до следующего скачивания агента.
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
            if _on_installed is not None:
                try:
                    await _on_installed(gw.id)
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
            if _on_applied is not None and not res.get("dup"):
                try:
                    await _on_applied(gw.id, bool(res.get("ok")), str(res.get("error") or ""),
                                      str(res.get("fp") or ""))
                except Exception as e:                    # noqa: BLE001
                    log.warning("канал линка: итог применения слота %s не показан: %s", gw.id, e)
            return
        log.info("канал линка: слот %s прислал неизвестное %s — игнорирую", gw.id, repr(str(kind)[:32]))

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
            # drain без предела ждал TCP-сдачи обесточенной малины ~15 минут
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
        if digest == sess.own_have or digest == sess.own_sent:
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
                    name = dev.name + (f" · {g.label}" if getattr(g, "label", "") else "")
            elif dev is not None:
                standby = True
        return {"standby": standby, "name": name}

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
        payload = {"active": is_active, **await asyncio.to_thread(self._slot_words, slot_id)}
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
                await self.send_role(slot_id)
            except Exception as e:                        # noqa: BLE001
                log.warning("канал линка: доставка слоту %s: %s", slot_id, e)


# ── единственный экземпляр на процесс ────────────────────────────────────────

_server: LinkServer | None = None


_on_applied = None
_on_installed = None


def set_on_installed(fn) -> None:
    """Корутина `fn(slot_id)` — сказать админу, что новый шлюз настроен и на
    связи (вид `installed` от агента). Ставит main: у слушателя канала своего
    бота нет."""
    global _on_installed
    _on_installed = fn


def set_on_applied(fn) -> None:
    """Корутина `fn(slot_id, ok, error, fp)` — показать итог применения в чате
    админа (fp — отпечаток применённого файла, "" у старого агента). Ставит
    main: у слушателя канала своего бота нет."""
    global _on_applied
    _on_applied = fn


async def ensure(services) -> LinkServer | None:
    """Поднять/перевесить слушатели. Роль gateway сюда не заходит вовсе.
    Сессии прошлого процесса сбрасывает старт бота (main, до поллинга)."""
    global _server
    if config.ROLE == "gateway":
        return None
    if _server is None:
        _server = LinkServer(services)
    await _server.ensure()
    await _server.deliver_all()
    return _server


async def liveness_tick(services) -> None:
    """Такт живости (30 с): слушатели по слотам, мёртвые сессии долой, роль —
    и только каждый четвёртый такт полная сверка доставки в отдельной задаче:
    ~20 запросов к БД на сессию каждые 30 с ради редкого расхождения — лишнее."""
    global _server
    if config.ROLE == "gateway":
        return
    if _server is None:
        _server = LinkServer(services)
    await _server.ensure()
    await _server.sweep_dead()
    await _server.send_roles()
    n = _server.__dict__.get("_ticks", 0) + 1
    _server.__dict__["_ticks"] = n
    if n % 4 == 0:
        _server.deliver_soon()


async def monitor_tick(services) -> None:
    """Тик монитора (минуты): полная сверка доставки отдельной задачей."""
    if config.ROLE == "gateway" or _server is None:
        return
    _server.deliver_soon()


def current() -> LinkServer | None:
    return _server


async def shutdown() -> None:
    global _server
    if _server is not None:
        await _server.stop()
        _server = None
