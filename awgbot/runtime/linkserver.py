"""
linkserver.py — сторона ВПС для канала ВПС ↔ шлюз (концепт «канал линка»).

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
Ключ читается заново на каждую сессию: после замены машины в слоте ключ линка
новый, а номер слота тот же, и кеш по номеру держал бы канал мёртвым до
перезапуска бота.

Периодического обмена здесь нет вовсе: сессия держится открытой, TCP-keepalive
намеренно не включаем (мапинг NAT держит keepalive самого туннеля). Простой —
ноль пакетов; см. §4.3 концепта.

Доставка на шлюз (этапы 2–4) — по расхождению, а не по событию: на каждом
снимке и в такте живости (`deliver_all` из `ensure`) сверяется локально, что
разошлось, и по сети уходит только это — настройки (`deliver`), фиды локальной
сети (`deliver_lists`), роль слота (`send_role`). Один и тот же набор за сессию
повторно не уходит. Диагностику и свежий снимок по запросу человека дают
`ask_tail` и `ask_snap`.

Байты канала по слоту копятся в `_gwlink_io`: зонд живости по уликам линка
вычитает их, чтобы снимки, фиды и диагностика не сходили за обратный трафик
клиентов.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from awgbot.core import config, settings
from awgbot.domain import gwsnapshot
from awgbot.util import gwlink

log = logging.getLogger(__name__)

DEFAULT_PORT = gwlink.DEFAULT_PORT


def channel_port() -> int:
    return settings.get_int("app.routing.link_channel_port", gwlink.DEFAULT_PORT)


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
        self.role_sent: bool | None = None
        # вытеснила сессию, прошедшую hello: запись «на связи» в БД — её, и
        # закрыть её обязана эта, даже если сама до hello не дойдёт
        self.took_over = False


class LinkServer:
    """По серверу на адрес линка, по сессии на слот."""

    def __init__(self, services):
        self.services = services
        self._servers: dict[tuple[str, int], asyncio.AbstractServer] = {}
        self._sessions: dict[int, _Session] = {}
        # Сколько снимков принято по слоту за жизнь процесса: «Обновить»
        # сравнивает до и после. Строка времени в state для этого не годится —
        # она с точностью до секунды, и снимок в ту же секунду был бы «не пришёл».
        self.snaps_in: dict[int, int] = {}
        self.tails: dict[int, tuple[str, str]] = {}
        self.tails_in: dict[int, int] = {}

    # ── байты канала: чтобы не выдавать их за обратный трафик клиентов ──────

    def _io(self, slot_id: int, rx: int = 0, tx: int = 0) -> None:
        """Счёт байтов канала по слоту поверх счётчиков линка. Зонд живости по
        уликам вычитает их: иначе диагностика, фиды или снимки, прошедшие тем же
        линком, сходили бы за обратный трафик клиентов, и автомат считал бы путь
        наружу живым ровно тогда, когда человек разбирается со сломанным слотом."""
        io = self.services.__dict__.setdefault("_gwlink_io", {})
        cur = io.get(slot_id) or [0, 0, 0]
        io[slot_id] = [cur[0] + rx, cur[1] + tx, cur[2] + 1]

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

    async def _drop(self, slot_id: int) -> None:
        sess = self._sessions.pop(slot_id, None)
        if sess is None:
            return
        with contextlib.suppress(Exception):
            sess.writer.close()
            await sess.writer.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("", 0)
        local = writer.get_extra_info("sockname") or ("", 0)
        gw = self._slot_for(str(local[0]), str(peer[0]))
        if gw is None:
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
        old = self._sessions.get(gw.id)
        await self._drop(gw.id)                           # вторая сессия вытесняет первую
        sess = _Session(writer, key)
        sess.took_over = bool(old and (old.hello or old.took_over))
        self._sessions[gw.id] = sess
        last_seq: int | None = None
        try:
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
                    msg = gwlink.unpack(key, line, last_seq=last_seq)
                except gwlink.ProtocolError as e:
                    log.warning("канал линка: слот %s — %s", gw.id, e)
                    await asyncio.to_thread(self.services.gwlink_note_error, gw.id, str(e))
                    break
                last_seq = int(msg.get("seq") or 0)
                await self._handle(gw, sess, msg)
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as e:
            log.info("канал линка: сессия слота %s закрыта (%s)", gw.id, e)
        finally:
            if self._sessions.get(gw.id) is sess:
                self._sessions.pop(gw.id, None)
                # «Последний раз на связи» — только для сессии, которая была:
                # отвергнутый на подписи или окне времени коннект не освежает
                # отметку, иначе напоминания молчали бы вечно, хотя канал не
                # доставил ни разу
                if sess.hello or sess.took_over:
                    await asyncio.to_thread(self.services.gwlink_session_closed, gw.id)
            with contextlib.suppress(Exception):
                writer.close()

    # ── разбор сообщений ─────────────────────────────────────────────────────

    async def _handle(self, gw, sess: _Session, msg: dict) -> None:
        kind = msg.get("t")
        if kind == "hello":
            proto = int(msg.get("proto") or 0)
            sess.hello = True
            await asyncio.to_thread(self.services.gwlink_session_opened, gw.id,
                                    str(msg.get("agent") or "")[:32], proto)
            sess.lists_have = str(msg.get("lists_hash") or "")[:64]
            # роль — первым сообщением сервера: по нему клиент понимает, что
            # сессия настоящая, и только тогда сбрасывает свой бэкофф
            await self.send_role(gw.id)
            await self.deliver_lists(gw.id)
            if proto != gwlink.PROTO:
                log.info("канал линка: слот %s говорит proto=%s, у нас %s",
                         gw.id, proto, gwlink.PROTO)
            return
        if not sess.hello:
            log.info("канал линка: слот %s прислал «%s» до hello — игнорирую", gw.id, kind)
            return
        if kind in ("snap", "delta"):
            patch = gwsnapshot.sanitize(msg)
            rev = int(msg.get("rev") or 0)
            ok = await asyncio.to_thread(self.services.gwlink_snapshot_in, gw.id, patch,
                                         rev, kind == "snap")
            if ok:
                self.snaps_in[gw.id] = self.snaps_in.get(gw.id, 0) + 1
                # снимок показал, что стоит на шлюзе, — самое время доставить
                await self.deliver(gw.id)
            else:
                # Разрыв нумерации: дельта потерялась или пришла не по порядку.
                # Просим полный снимок и начинаем счёт заново.
                await self.send(gw.id, "ask", {"what": "snap"})
            return
        if kind == "tail":
            name = str(msg.get("name") or "")[:16]
            self.tails[gw.id] = (name, str(msg.get("text") or "")[:4000])
            self.tails_in[gw.id] = self.tails_in.get(gw.id, 0) + 1
            return
        if kind == "lists_ack":
            if msg.get("ok"):
                sess.lists_have = str(msg.get("hash") or "")[:64]
            await asyncio.to_thread(self.services.gwlink_lists_ack_in, gw.id, msg)
            return
        if kind == "ack":
            await asyncio.to_thread(self.services.gwlink_ack_in, gw.id, msg)
            if msg.get("ok") and "LAN_MODE" in (msg.get("changed") or []):
                # Режим без VPN только что включился каналом: фиды, отправленные
                # при hello, шлюз отверг («режим выключен»), а повторно за сессию
                # они не уходят. Теперь есть кому их принять — шлём заново, иначе
                # квартира ждала бы своих фидов до следующего скачивания агента.
                sess.lists_sent = ""
                await self.deliver_lists(gw.id)
            return
        if kind == "claim":
            token = str(msg.get("token") or "")[:4096]
            await asyncio.to_thread(self.services.gwlink_claim_in, gw.id, token)
            return
        log.info("канал линка: слот %s прислал неизвестное «%s» — игнорирую", gw.id, kind)

    async def send(self, slot_id: int, kind: str, body: dict | None = None,
                   pad: int = gwlink.PAD_DELTA) -> bool:
        sess = self._sessions.get(slot_id)
        if sess is None:
            return False
        sess.seq += 1
        line = gwlink.pack(sess.key, kind, body, seq=sess.seq, pad=pad)
        try:
            sess.writer.write(line)
            await sess.writer.drain()
            self._io(slot_id, tx=len(line))
            return True
        except (ConnectionError, OSError) as e:
            log.info("канал линка: слот %s не принял «%s» (%s)", slot_id, kind, e)
            await self._drop(slot_id)
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

    async def send_role(self, slot_id: int) -> bool:
        """Сказать агенту, активный он или резерв, — только при смене. Сам он
        этого знать не может: решает автомат переключения здесь, на ВПС."""
        sess = self._sessions.get(slot_id)
        if sess is None:
            return False
        active = await asyncio.to_thread(self.services.active_gateway)
        is_active = active is not None and active.id == slot_id
        if sess.role_sent == is_active:
            return False
        sess.role_sent = is_active
        return await self.send(slot_id, "role", {"active": is_active})

    async def _ask(self, slot_id: int, body: dict, counter: dict, timeout: float) -> bool:
        before = counter.get(slot_id, 0)
        if not await self.send(slot_id, "ask", body):
            return False
        for _ in range(max(1, int(timeout / 0.25))):
            await asyncio.sleep(0.25)
            if counter.get(slot_id, 0) != before:
                return True
        return False

    async def ask_snap(self, slot_id: int, timeout: float = 3.0) -> bool | None:
        """Попросить полный снимок и дождаться. None — сессии нет; False —
        шлюз не ответил вовремя."""
        if slot_id not in self._sessions:
            return None
        return await self._ask(slot_id, {"what": "snap"}, self.snaps_in, timeout)

    async def ask_tail(self, slot_id: int, name: str, timeout: float = 6.0) -> str | None:
        """Попросить у шлюза диагностику по имени из закрытого списка и дождаться.
        None — шлюз не на связи или не ответил вовремя."""
        if not await self._ask(slot_id, {"what": "tail", "name": name}, self.tails_in, timeout):
            return None
        got = self.tails.get(slot_id)
        return got[1] if got and got[0] == name else None

    async def deliver_all(self) -> None:
        """Такт живости: у каждой живой сессии проверить, нечего ли доставить.
        Локальная сверка в БД; по сети уходит только то, что разошлось."""
        for slot_id in list(self._sessions):
            try:
                await self.deliver(slot_id)
                await self.deliver_lists(slot_id)
                await self.send_role(slot_id)
            except Exception as e:                        # noqa: BLE001
                log.warning("канал линка: доставка слоту %s: %s", slot_id, e)


# ── единственный экземпляр на процесс ────────────────────────────────────────

_server: LinkServer | None = None


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


def current() -> LinkServer | None:
    return _server


async def shutdown() -> None:
    global _server
    if _server is not None:
        await _server.stop()
        _server = None
