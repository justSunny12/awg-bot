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

Доставка на шлюз (этапы 2–4) — по расхождению, а не по событию: на каждом
снимке и в такте живости (`deliver_all` из `ensure`) сверяется локально, что
разошлось, и по сети уходит только это — настройки (`deliver`), фиды локальной
сети (`deliver_lists`), роль слота (`send_role`). Один и тот же набор за сессию
повторно не уходит. Диагностику по запросу человека даёт `ask_tail`.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
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
        # Отпечаток набора настроек, уже отправленного в ЭТОЙ сессии. Тот же
        # набор повторно не шлём: неудачное применение иначе рестартило бы
        # обвязку шлюза каждым тактом живости. Новая сессия — новая попытка.
        self._sent_hash: dict[int, str] = {}
        # То же для фидов локальной сети: какой отпечаток шлюз назвал своим
        # (hello, ответ на доставку) и какой уже ушёл в этой сессии.
        self._lists_have: dict[int, str] = {}
        self._lists_sent: dict[int, str] = {}
        # Роль, уже сообщённая слоту в этой сессии, и принятые хвосты диагностики.
        self._role_sent: dict[int, bool] = {}
        self.tails: dict[int, tuple[str, str]] = {}
        self.tails_in: dict[int, int] = {}

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
        self._sent_hash.pop(slot_id, None)
        self._lists_have.pop(slot_id, None)
        self._lists_sent.pop(slot_id, None)
        self._role_sent.pop(slot_id, None)
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
                self._sent_hash.pop(gw.id, None)
                self._lists_have.pop(gw.id, None)
                self._lists_sent.pop(gw.id, None)
                self._role_sent.pop(gw.id, None)
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
            self._lists_have[gw.id] = str(msg.get("lists_hash") or "")[:64]
            await self.deliver_lists(gw.id)
            await self.send_role(gw.id)
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
                # снимок показал, что стоит на шлюзе, — самое время доставить
                await self.deliver(gw.id)
            if not ok:
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
                self._lists_have[gw.id] = str(msg.get("hash") or "")[:64]
            await asyncio.to_thread(self.services.gwlink_lists_ack_in, gw.id, msg)
            return
        if kind == "ack":
            await asyncio.to_thread(self.services.gwlink_ack_in, gw.id, msg)
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

    async def deliver(self, slot_id: int) -> bool:
        """Отправить настройки слоту, если снимок расходится с выдаваемым и этот
        набор в сессии ещё не отправляли. Возвращает, ушло ли сообщение."""
        if slot_id not in self._sessions:
            return False
        gw = await asyncio.to_thread(self.services.db.gateway, slot_id)
        if gw is None:
            return False
        want = await asyncio.to_thread(self.services.gwlink_settings_due, gw)
        if not want:
            return False
        digest = gwlink.settings_hash(want)
        if self._sent_hash.get(slot_id) == digest:
            return False
        self._sent_hash[slot_id] = digest
        log.info("канал линка: слоту %s уходят настройки", slot_id)
        return await self.send(slot_id, "settings", {"values": want})

    async def deliver_lists(self, slot_id: int) -> bool:
        """Отвезти фиды локальной сети, если у шлюза включён режим без VPN, а
        его отпечаток фидов не совпадает с нашим. Один раз за сессию на набор:
        отвергнутый шлюзом фид не шлём снова до новой сессии или новых фидов."""
        import base64
        import zlib
        if slot_id not in self._sessions:
            return False
        gw = await asyncio.to_thread(self.services.db.gateway, slot_id)
        if gw is None or not gw.lan_mode:
            return False
        feeds = await asyncio.to_thread(self.services.gwlink_lan_feeds)
        digest = feeds.get("hash") or ""
        if not digest or self._lists_sent.get(slot_id) == digest:
            return False
        if self._lists_have.get(slot_id) == digest:
            # У шлюза те же фиды. Подтверждаем один раз за сессию — по событию
            # подключения, а не по часам: иначе его запас на своё скачивание
            # протухал бы через 12 часов неизменных фидов, и адрес квартиры
            # снова пошёл бы на GitHub.
            self._lists_sent[slot_id] = digest
            return await self.send(slot_id, "lists_ok", {"hash": digest})
        self._lists_sent[slot_id] = digest
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
        if slot_id not in self._sessions:
            return False
        active = await asyncio.to_thread(self.services.active_gateway)
        is_active = active is not None and active.id == slot_id
        if self._role_sent.get(slot_id) == is_active:
            return False
        self._role_sent[slot_id] = is_active
        return await self.send(slot_id, "role", {"active": is_active})

    async def ask_tail(self, slot_id: int, name: str, timeout: float = 6.0) -> str | None:
        """Попросить у шлюза диагностику по имени из закрытого списка и дождаться.
        None — шлюз не на связи или не ответил вовремя."""
        before = self.tails_in.get(slot_id, 0)
        if not await self.send(slot_id, "ask", {"what": "tail", "name": name}):
            return None
        for _ in range(int(timeout / 0.25)):
            await asyncio.sleep(0.25)
            if self.tails_in.get(slot_id, 0) != before:
                got = self.tails.get(slot_id)
                return got[1] if got and got[0] == name else None
        return None

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
    """Поднять/перевесить слушатель. Роль gateway сюда не заходит вовсе."""
    global _server
    if config.ROLE == "gateway":
        return None
    if _server is None:
        # Первый подъём в этом процессе: сессии прежнего процесса мертвы.
        await asyncio.to_thread(services.gwlink_sessions_reset)
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
