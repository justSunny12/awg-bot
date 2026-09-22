"""
linkserver.py — сторона ВПС для канала ВПС ↔ шлюз (концепт «канал линка», этап 1).

Слушатель живёт ТОЛЬКО на адресах линков: публичный адрес сервера порт канала
не показывает даже при снятом файерволе. Коннект всегда устанавливает шлюз —
на малине не появляется ни одного слушающего сокета, и механика одинаково
работает за любым NAT.

Слот определяется парой «на какой локальный адрес пришли» и «каким ключом линка
проверилась подпись»: обе должны указывать на один слот, иначе разрыв. Вторая
сессия того же слота вытесняет первую — малина перезагрузилась, старая висит.

Периодического обмена здесь нет вовсе: сессия держится открытой, TCP-keepalive
намеренно не включаем (мапинг NAT держит keepalive самого туннеля). Простой —
ноль пакетов; см. §4.3 концепта.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging

from awgbot.core import config, settings
from awgbot.domain import gwsnapshot
from awgbot.util import gwlink

log = logging.getLogger(__name__)

DEFAULT_PORT = 8787


def channel_port() -> int:
    return settings.get_int("app.routing.link_channel_port", DEFAULT_PORT)


def vps_address(link_cidr: str) -> str:
    """Адрес ВПС в /30 линка — первый хост, как его считает скрипт линка."""
    try:
        hosts = list(ipaddress.ip_network(link_cidr, strict=False).hosts())
    except ValueError:
        return ""
    return str(hosts[0]) if hosts else ""


def gw_address(link_cidr: str) -> str:
    """Адрес шлюза в /30 линка — второй хост."""
    try:
        hosts = list(ipaddress.ip_network(link_cidr, strict=False).hosts())
    except ValueError:
        return ""
    return str(hosts[1]) if len(hosts) > 1 else ""


class LinkServer:
    """Один слушатель на все слоты, по сессии на слот."""

    def __init__(self, services):
        self.services = services
        self._server: asyncio.AbstractServer | None = None
        self._bound: tuple[tuple[str, int], ...] = ()
        self._sessions: dict[int, asyncio.StreamWriter] = {}
        self._keys: dict[int, bytes] = {}
        self._seq: dict[int, int] = {}
        # Сколько снимков принято по слоту за жизнь процесса: «Обновить»
        # сравнивает до и после. Строка времени в state для этого не годится —
        # она с точностью до секунды, и снимок в ту же секунду был бы «не пришёл».
        self.snaps_in: dict[int, int] = {}

    # ── жизненный цикл ───────────────────────────────────────────────────────

    def _wanted(self) -> tuple[tuple[str, int], ...]:
        port = channel_port()
        out = []
        for gw in self.services.db.gateways():
            addr = vps_address(gw.link_cidr)
            if addr:
                out.append((addr, port))
        return tuple(sorted(out))

    async def ensure(self) -> None:
        """Поднять слушатель или перевесить его, если состав слотов сменился.

        Зовётся со старта и с такта живости: слот могли завести, снять или
        переназначить, а привязка к адресу существует только пока поднят линк.
        Сравнение с уже поднятым делает вызов дешёвым — обычно это один
        list(db.gateways()) и выход.
        """
        want = self._wanted()
        if want == self._bound and self._server is not None:
            return
        await self.stop()
        if not want:
            return
        try:
            self._server = await asyncio.start_server(
                self._serve, [h for h, _ in want], want[0][1], limit=gwlink.MAX_LINE)
        except OSError as e:
            # Адрес линка ещё не поднят (ребут ВПС, линк не встал) — не ошибка
            # бота: следующий такт попробует снова.
            log.info("канал линка: слушатель не поднят (%s)", e)
            self._server = None
            return
        self._bound = want
        log.info("канал линка: слушаю %s", ", ".join(f"{h}:{p}" for h, p in want))

    async def stop(self) -> None:
        for slot_id in list(self._sessions):
            await self._drop(slot_id)
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        self._server = None
        self._bound = ()

    # ── сессии ───────────────────────────────────────────────────────────────

    def _slot_for(self, local_ip: str, peer_ip: str):
        for gw in self.services.db.gateways():
            if vps_address(gw.link_cidr) == local_ip and gw_address(gw.link_cidr) == peer_ip:
                return gw
        return None

    def _key(self, gw) -> bytes:
        key = self._keys.get(gw.id)
        if key is None:
            key = gwlink.channel_key(self.services._link_privkey(gw))
            self._keys[gw.id] = key
        return key

    async def _drop(self, slot_id: int) -> None:
        writer = self._sessions.pop(slot_id, None)
        self._seq.pop(slot_id, None)
        if writer is None:
            return
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()

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
            key = self._key(gw)
        except Exception as e:                            # noqa: BLE001
            log.warning("канал линка: ключ слота %s не прочитан: %s", gw.id, e)
            writer.close()
            return
        await self._drop(gw.id)                           # вторая сессия вытесняет первую
        self._sessions[gw.id] = writer
        last_seq: int | None = None
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                if len(line) > gwlink.MAX_LINE:
                    log.warning("канал линка: слот %s прислал строку сверх предела", gw.id)
                    break
                try:
                    msg = gwlink.unpack(key, line, last_seq=last_seq)
                except gwlink.ProtocolError as e:
                    log.warning("канал линка: слот %s — %s", gw.id, e)
                    await asyncio.to_thread(self.services.gwlink_note_error, gw.id, str(e))
                    break
                last_seq = int(msg.get("seq") or 0)
                await self._handle(gw, msg)
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as e:
            log.info("канал линка: сессия слота %s закрыта (%s)", gw.id, e)
        finally:
            if self._sessions.get(gw.id) is writer:
                self._sessions.pop(gw.id, None)
                self._seq.pop(gw.id, None)
                await asyncio.to_thread(self.services.gwlink_session_closed, gw.id)
            with contextlib.suppress(Exception):
                writer.close()

    # ── разбор сообщений ─────────────────────────────────────────────────────

    async def _handle(self, gw, msg: dict) -> None:
        kind = msg.get("t")
        if kind == "hello":
            proto = int(msg.get("proto") or 0)
            await asyncio.to_thread(self.services.gwlink_session_opened, gw.id,
                                    str(msg.get("agent") or "")[:32], proto)
            if proto != gwlink.PROTO:
                log.info("канал линка: слот %s говорит proto=%s, у нас %s",
                         gw.id, proto, gwlink.PROTO)
            return
        if kind in ("snap", "delta"):
            patch = gwsnapshot.sanitize(msg)
            rev = int(msg.get("rev") or 0)
            ok = await asyncio.to_thread(self.services.gwlink_snapshot_in, gw.id, patch,
                                         rev, kind == "snap")
            if ok:
                self.snaps_in[gw.id] = self.snaps_in.get(gw.id, 0) + 1
            if not ok:
                # Разрыв нумерации: дельта потерялась или пришла не по порядку.
                # Просим полный снимок и начинаем счёт заново.
                await self.send(gw.id, "ask", {"what": "snap"})
            return
        if kind == "claim":
            token = str(msg.get("token") or "")[:4096]
            await asyncio.to_thread(self.services.gwlink_claim_in, gw.id, token)
            return
        log.info("канал линка: слот %s прислал неизвестное «%s» — игнорирую", gw.id, kind)

    async def send(self, slot_id: int, kind: str, body: dict | None = None) -> bool:
        writer = self._sessions.get(slot_id)
        if writer is None:
            return False
        try:
            key = self._keys[slot_id]
        except KeyError:
            return False
        self._seq[slot_id] = self._seq.get(slot_id, 0) + 1
        line = gwlink.pack(key, kind, body, seq=self._seq[slot_id], pad=gwlink.PAD_DELTA)
        try:
            writer.write(line)
            await writer.drain()
            return True
        except (ConnectionError, OSError) as e:
            log.info("канал линка: слот %s не принял «%s» (%s)", slot_id, kind, e)
            await self._drop(slot_id)
            return False

    def online(self, slot_id: int) -> bool:
        return slot_id in self._sessions


# ── единственный экземпляр на процесс ────────────────────────────────────────

_server: LinkServer | None = None


async def ensure(services) -> LinkServer | None:
    """Поднять/перевесить слушатель. Роль gateway сюда не заходит вовсе."""
    global _server
    if config.ROLE == "gateway":
        return None
    if _server is None:
        # Первый подъём в этом процессе: сессии прежнего процесса мертвы.
        await asyncio.to_thread(services.gwlink_sessions_reset)
        _server = LinkServer(services)
    await _server.ensure()
    return _server


def current() -> LinkServer | None:
    return _server


async def shutdown() -> None:
    global _server
    if _server is not None:
        await _server.stop()
        _server = None
