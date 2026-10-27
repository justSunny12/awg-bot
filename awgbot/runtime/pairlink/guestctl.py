"""
guestctl.py — локальный канал между основным ботом резерва и ботом основы на
резерве (гостевой режим): unix-сокет `/run/awg-bot/rsv/guest.sock`, слушает
бот резерва, подключается гость (сокет вмонтирован в его песочницу).

Строки JSON, без подписи: сокет локальный, 0600, владелец root; в
пространство гостя попадает только он. Гость по нему узнаёт состояние основы
глазами резерва (Т-РГ1, Т-РГ3, кнопка «↩️ Вернуть пользователей»), получает
команду «ставь вебхук» (факт записи) и «замолчи и сними реплику» (возврат),
отдаёт число устройств на связи, нажатие кнопки возврата и шифротексты
реплики. По сети гость с основой не говорит — всё через бот резерва.

Гость → резерв: hello{online}, online{n}, tg_taken{}, back_req{}, replica{blob}, blocks{addrs}, bye{}
(blocks — адреса заблокированных устройств по решениям гостя: резерв шлёт их основе, та
держит DROP своим оставшимся сессиям).
Резерв → гость: state{name, since, reason, primary, return, self_host},
take_tg{}, blocks{addrs}, stop{} (гость пишет финальный снимок, шлёт replica и выходит).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import time

log = logging.getLogger(__name__)

MAX_LINE = 64 * 1024 * 1024 + 1024     # реплика шифротекстом в одной строке
_BACKOFF = (2, 5, 10, 20, 30)


def _dumps(kind: str, body: dict | None = None) -> bytes:
    return (json.dumps({"kind": kind, **(body or {})}, ensure_ascii=False) + "\n").encode()


def _loads(line: bytes) -> dict | None:
    try:
        msg = json.loads(line.decode())
    except (ValueError, UnicodeDecodeError):
        return None
    return msg if isinstance(msg, dict) and isinstance(msg.get("kind"), str) else None


# ── сторона бота резерва: слушатель ──────────────────────────────────────────

class GuestCtlServer:
    """Слушатель локального канала у основного бота резерва; одна сессия гостя."""

    def __init__(self, services, path: str = ""):
        from awgbot.infra import rsvzone
        self.services = services
        self.path = path or str(rsvzone.GUEST_SOCK)
        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.online_count: int = 0
        self.online_at: float = 0.0
        self.replica: bytes | None = None          # последняя реплика от гостя (шифротекст)
        self.back_requested_at: float = 0.0
        self._state_sent: str = ""
        self._words: list[tuple] = []           # слова гостя для такта: (вид, тело)

    def take_words(self) -> list[tuple]:
        words, self._words = self._words, []
        return words

    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def ensure(self) -> None:
        if self._server is not None:
            return
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with contextlib.suppress(OSError):
            os.unlink(self.path)
        old = os.umask(0o177)                                 # файл сокета — 0600 с создания, не после chmod
        try:
            self._server = await asyncio.start_unix_server(self._serve, path=self.path, limit=MAX_LINE)
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        log.info("локальный канал гостя: слушаю %s", self.path)

    async def stop(self) -> None:
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
            self._writer = None
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(OSError):
            os.unlink(self.path)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._writer is not None and not self._writer.is_closing():
            with contextlib.suppress(Exception):
                self._writer.close()
        self._writer = writer
        self._state_sent = ""
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                msg = _loads(line)
                if msg is None:
                    continue
                await self._handle(msg)
        except (asyncio.IncompleteReadError, ConnectionError, ValueError, OSError) as e:
            log.debug("локальный канал гостя: сессия закрыта: %s", e)
        finally:
            if self._writer is writer:
                self._writer = None
            with contextlib.suppress(Exception):
                writer.close()

    async def _handle(self, msg: dict) -> None:
        kind = msg["kind"]
        if kind in ("hello", "online"):
            self.online_count = int(msg.get("online", msg.get("n", 0)) or 0)
            self.online_at = time.time()
            if kind == "hello":
                await self.push_state(force=True)
        elif kind == "back_req":
            self.back_requested_at = time.time()
            self._words.append(("back_req", None))
        elif kind == "tg_taken":
            self._words.append(("tg_taken", None))
            await self.send("ack", {"of": "tg_taken"})      # гость повторяет слово, пока не подтверждено
        elif kind == "webhook":
            self._words.append(("webhook", {"stalled": bool(msg.get("stalled")), "since": str(msg.get("since") or ""),
                                            "error": str(msg.get("error") or "")}))
        elif kind == "replica":
            try:
                self.replica = bytes.fromhex(str(msg.get("blob") or ""))
            except ValueError:
                self.replica = None
            if self.replica:
                self._words.append(("replica", self.replica))
        elif kind == "blocks":
            self._words.append(("blocks", [str(a) for a in (msg.get("addrs") or [])][:4096]))
        elif kind == "bye":
            pass

    async def send(self, kind: str, body: dict | None = None) -> bool:
        w = self._writer
        if w is None or w.is_closing():
            return False
        try:
            w.write(_dumps(kind, body))
            await w.drain()
            return True
        except (ConnectionError, OSError):
            return False

    async def push_state(self, force: bool = False) -> bool:
        """Состояние основы глазами резерва — гостю; по смене или force."""
        try:
            view = await asyncio.to_thread(self.services.reserve_guest_state_view)
        except Exception as e:                               # noqa: BLE001
            log.warning("локальный канал гостя: состояние не собралось: %s", e)
            return False
        packed = json.dumps(view, sort_keys=True, ensure_ascii=False)
        if not force and packed == self._state_sent:
            return True
        if await self.send("state", view):
            self._state_sent = packed
            return True
        return False


# ── сторона гостя: клиент ────────────────────────────────────────────────────

class GuestCtlClient:
    """Клиент локального канала в боте основы на резерве: подключение с
    отступом, состояние — в сервисы, команды — события."""

    def __init__(self, services, path: str = ""):
        from awgbot.infra import rsvzone
        self.services = services
        self.path = path or str(rsvzone.GUEST_SOCK)
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self.take_tg = asyncio.Event()
        self.tg_acked = asyncio.Event()                      # резерв подтвердил tg_taken
        self.stop_req = asyncio.Event()
        self.blocks: list[str] | None = None    # последние блокировки от основы (через резерв)
        self._attempt = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
            self._writer = None

    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def _run(self) -> None:
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(self.path, limit=MAX_LINE)
            except (OSError, ConnectionError):
                delay = _BACKOFF[min(self._attempt, len(_BACKOFF) - 1)]
                self._attempt += 1
                await asyncio.sleep(delay + random.uniform(0, delay * 0.3))
                continue
            self._attempt = 0
            self._writer = writer
            try:
                await self.send("hello", {"online": self.services.guest_devices_online()})
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    msg = _loads(line)
                    if msg is not None:
                        await self._handle(msg)
            except (asyncio.IncompleteReadError, ConnectionError, ValueError, OSError) as e:
                log.debug("гость: локальный канал закрыт: %s", e)
            finally:
                self._writer = None
                with contextlib.suppress(Exception):
                    writer.close()
            await asyncio.sleep(2)

    async def _handle(self, msg: dict) -> None:
        kind = msg["kind"]
        if kind == "state":
            await asyncio.to_thread(self.services.guest_state_in, msg)
        elif kind == "ack":
            if str(msg.get("of") or "") == "tg_taken":
                self.tg_acked.set()
        elif kind == "take_tg":
            self.take_tg.set()
        elif kind == "stop":
            self.stop_req.set()
        elif kind == "blocks":
            self.blocks = [str(a) for a in (msg.get("addrs") or [])][:4096]
            await asyncio.to_thread(self.services.guest_apply_blocks, self.blocks)

    async def send(self, kind: str, body: dict | None = None) -> bool:
        w = self._writer
        if w is None or w.is_closing():
            return False
        try:
            w.write(_dumps(kind, body))
            await w.drain()
            return True
        except (ConnectionError, OSError):
            return False

    async def send_online(self) -> bool:
        return await self.send("online", {"n": await asyncio.to_thread(self.services.guest_devices_online)})

    async def send_replica(self, blob: bytes) -> bool:
        return await self.send("replica", {"blob": blob.hex()})


_server: GuestCtlServer | None = None
_client: GuestCtlClient | None = None


def set_client(c: "GuestCtlClient | None") -> None:
    global _client
    _client = c


def client() -> "GuestCtlClient | None":
    return _client


async def ensure(services) -> GuestCtlServer | None:
    """Сторона бота резерва: слушатель, пока есть пара; снять — без пары."""
    global _server
    if not services.reserve_paired():
        if _server is not None:
            await _server.stop()
            _server = None
        return None
    if _server is None:
        _server = GuestCtlServer(services)
    try:
        await _server.ensure()
    except OSError as e:
        log.warning("локальный канал гостя: слушатель не поднялся: %s", e)
        return _server
    return _server


def server() -> GuestCtlServer | None:
    return _server


async def shutdown() -> None:
    global _server
    if _server is not None:
        await _server.stop()
        _server = None
