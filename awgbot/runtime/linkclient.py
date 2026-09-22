"""
linkclient.py — сторона шлюза для канала ВПС ↔ шлюз (концепт «канал линка», этап 1).

Коннект устанавливает ТОЛЬКО малина: на ней не появляется ни одного слушающего
сокета, и механика работает за любым NAT. Ответные пакеты сервера проходят в
`tunnel_in` по `ct state established` — на шлюзе не нужно ни одного нового
правила файервола.

Периодической задачи здесь нет. Сессия держится открытой, и в простое по ней не
уходит ни байта: снимок отправляется при подключении, дальше — только когда
факт или вердикт действительно сменились. Переподключение — с бэкоффом и
джиттером: ровный ритм попыток был бы тем же маячком, только на уровне TCP.

Канал включается тем, что привёз бандл (`LINK_CHANNEL=1` в юните обвязки): без
перевыпуска конфигурации агент никуда не ходит — это и есть рубильник.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import random
import subprocess

from awgbot.core import config
from awgbot.domain import gwsnapshot
from awgbot.util import gwlink

log = logging.getLogger(__name__)

DEFAULT_PORT = 8787
# 5 с → 15 → 45 → 135 → 300, каждый шаг × uniform(0.6, 1.4). Потолок пять минут:
# линк лежит редко и надолго, а долбиться в него чаще незачем.
_BACKOFF = (5, 15, 45, 135, 300)


def enabled() -> bool:
    from awgbot.infra import gwguard
    return gwguard.unit_env("LINK_CHANNEL") == "1"


def server_port() -> int:
    from awgbot.infra import gwguard
    raw = gwguard.unit_env("LINK_CHANNEL_PORT")
    return int(raw) if raw.isdigit() else DEFAULT_PORT


def server_address() -> str:
    """Адрес ВПС в /30 линка: наш адрес спрашиваем у ядра, второй считается
    однозначно. Ядро достовернее конфига — его могли и не применить."""
    try:
        proc = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", config.GW_LINK_IF],
                              capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    for tok in proc.stdout.decode(errors="replace").split():
        if "/" not in tok or tok.count(".") != 3:
            continue
        try:
            iface = ipaddress.ip_interface(tok)
        except ValueError:
            continue
        if iface.network.prefixlen != 30:
            return ""
        peers = [h for h in iface.network.hosts() if h != iface.ip]
        return str(peers[0]) if peers else ""
    return ""


class LinkClient:
    def __init__(self, services):
        self.services = services
        self._task: asyncio.Task | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._key: bytes | None = None
        self._seq = 0
        self._rev = 0
        self._prev: dict = {}
        self._sent_bytes = 0                  # для тестов «в простое ноль пакетов»

    # ── соединение ───────────────────────────────────────────────────────────

    def _channel_key(self) -> bytes:
        if self._key is None:
            from awgbot.util import bundlecrypt
            with open(config.GW_LINK_CONF, encoding="utf-8") as f:
                self._key = gwlink.channel_key(bundlecrypt.read_privkey(f.read()))
        return self._key

    async def _connect_once(self) -> None:
        host, port = server_address(), server_port()
        if not host:
            raise OSError("адрес ВПС в линке не определён")
        reader, writer = await asyncio.open_connection(host, port, limit=gwlink.MAX_LINE)
        self._writer = writer
        self._seq = 0
        self._rev = 0
        self._prev = {}
        log.info("канал линка: подключился к %s:%s", host, port)
        try:
            await self._send("hello", {"proto": gwlink.PROTO,
                                       "agent": config.INSTALLED_VERSION})
            await self.push(full=True)
            last_seq: int | None = None
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = gwlink.unpack(self._channel_key(), line, last_seq=last_seq)
                except gwlink.ProtocolError as e:
                    log.warning("канал линка: сообщение с ВПС отвергнуто — %s", e)
                    break
                last_seq = int(msg.get("seq") or 0)
                await self._handle(msg)
        finally:
            self._writer = None
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _run(self) -> None:
        attempt = 0
        while True:
            try:
                await self._connect_once()
                attempt = 0                    # была живая сессия — счёт заново
            except asyncio.CancelledError:
                raise
            except Exception as e:             # noqa: BLE001
                # Молча: линк и так даёт свой алерт, а второй про то же — шум.
                log.info("канал линка: нет связи с ВПС (%s)", e)
            delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)] * random.uniform(0.6, 1.4)
            attempt += 1
            await asyncio.sleep(delay)

    # ── отправка ─────────────────────────────────────────────────────────────

    async def _send(self, kind: str, body: dict | None = None, pad: int = 0) -> bool:
        writer = self._writer
        if writer is None:
            return False
        self._seq += 1
        line = gwlink.pack(self._channel_key(), kind, body, seq=self._seq, pad=pad)
        try:
            writer.write(line)
            await writer.drain()
            self._sent_bytes += len(line)
            return True
        except (ConnectionError, OSError) as e:
            log.info("канал линка: отправка «%s» не прошла (%s)", kind, e)
            return False

    async def push(self, *, full: bool = False) -> bool:
        """Отправить снимок или дельту. Дельта пуста — не уходит НИЧЕГО: это
        обычное состояние канала на дни, а не особый случай."""
        if self._writer is None:
            return False
        try:
            snap = await asyncio.to_thread(self.services.gw_snapshot)
        except Exception as e:                            # noqa: BLE001
            log.warning("канал линка: снимок не собран: %s", e)
            return False
        if full or not self._prev:
            self._rev = 1
            self._prev = snap
            return await self._send("snap", {**snap, "rev": 1}, pad=gwlink.PAD_SNAP)
        patch = gwsnapshot.delta(self._prev, snap)
        if not patch:
            return False
        self._rev += 1
        self._prev = snap
        return await self._send("delta", {**patch, "rev": self._rev}, pad=gwlink.PAD_DELTA)

    async def _handle(self, msg: dict) -> None:
        if msg.get("t") == "ask" and msg.get("what") == "snap":
            await self.push(full=True)
            return
        log.info("канал линка: с ВПС пришло неизвестное «%s» — игнорирую", msg.get("t"))

    # ── жизненный цикл ───────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
            self._task.add_done_callback(
                lambda t: t.exception() if not t.cancelled() else None)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


_client: LinkClient | None = None


def ensure(services) -> LinkClient | None:
    """Поднять клиента, если канал включён бандлом. Роль client сюда не ходит."""
    global _client
    if config.ROLE != "gateway" or not enabled():
        return None
    if _client is None:
        _client = LinkClient(services)
    _client.start()
    return _client


def online() -> bool:
    """Сессия с ВПС сейчас открыта. Без сети: просто состояние объекта."""
    return _client is not None and _client._writer is not None


async def on_tick(services) -> None:
    """После тика монитора: отправить дельту, если что-то действительно
    изменилось. Зовётся из задачи планировщика, а не своим расписанием —
    своего расписания у канала нет по замыслу."""
    client = ensure(services)
    if client is not None:
        await client.push()


async def shutdown() -> None:
    global _client
    if _client is not None:
        await _client.stop()
        _client = None
