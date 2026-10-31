"""
guestlink.py — гостевой канал на стороне шлюза: клиент TCP внутри постоянного
линка `awgrsv` к стороне зоны резерва. Конверт — `util/gwlink` с ключом из
PSK линка (`pairproto.guest_key`), словарь — `pairproto.GUEST_KINDS`.

В простое ни байта: `hello` в начале сессии (слот, возможности), `pong` на
`ping`, слова кворума (`qview`, `qshare`); от резерва — `state`, `ping` и слова
кандидата (`q*`). Переподключение с
отступом и джиттером; линк восстановился — сразу.

Такт 5 с (`tick`): наблюдатели живости (domain/gateway/reserve/watch) и
исполнение их действий: проба основы по каналу конфигурации, слова резерву,
пересылка (скрипт линка к резерву), снимок серверу, уведомления.

Такт кворума (`quorum_tick`) — своим заданием планировщика: у наблюдателей
медленные шаги (подъём линка скриптом, DNS, уведомления, обход Telegram с
`getMe`), за которыми проба голосующего опоздала бы дольше `no_s` — ложное
«не вижу» кандидату. Слова кворума (qview резерву и серверу, снимок) уходят
фоном по очереди, проба их не ждёт.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random

from awgbot.util import gwlink, pairproto

log = logging.getLogger(__name__)

HELLO_TIMEOUT = 15
SEND_TIMEOUT = 20
_BACKOFF = (5, 10, 20, 40, 80, 160, 300)
_STORE_LOCK = asyncio.Lock()       # reserve.json правят такт и обработчик слов — не одновременно


class GuestClient:
    def __init__(self, services):
        self.services = services
        self._task: asyncio.Task | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._key: bytes = b""
        self._seq = 0
        self._cn = b""
        self._sn = b""
        self._wake = asyncio.Event()
        self._confirmed = False
        self.online = False

    # ── жизненный цикл ───────────────────────────────────────────────────────
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._close()

    def wake(self) -> None:
        self._wake.set()

    async def _close(self) -> None:
        w, self._writer = self._writer, None
        self.online = False
        if w is not None:
            with contextlib.suppress(Exception):
                w.close()

    async def _run(self) -> None:
        attempt = 0
        while True:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:                        # noqa: BLE001
                log.info("гостевой канал: нет связи (%s)", e)
            await self._close()
            if self._confirmed:
                attempt, self._confirmed = 0, False
            delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)] * random.uniform(0.6, 1.4)
            attempt += 1
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), delay)
                attempt = 0

    async def _connect_once(self) -> None:
        host, port = await asyncio.to_thread(self.services.reserve_guest_target)
        self._key = await asyncio.to_thread(self.services.reserve_guest_channel_key)
        if not host or not self._key:
            raise OSError("адрес или ключ гостевого канала не определены")
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, limit=gwlink.MAX_LINE), timeout=20)
        gwlink.tcp_keepalive(writer)
        self._seq, self._cn, self._sn = 0, gwlink.new_nonce(), b""
        body = await asyncio.to_thread(self.services.reserve_guest_hello_body)
        if not body:
            writer.close()
            raise OSError("взгляда на основу ещё нет — hello позже")
        body["nonce"] = gwlink.nonce_b64(self._cn)
        self._seq += 1
        writer.write(gwlink.pack(self._key, "hello", body, seq=self._seq, pad=gwlink.PAD_DELTA))
        await asyncio.wait_for(writer.drain(), SEND_TIMEOUT)
        last_seq = 0
        try:
            first = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT)
            if not first:
                return
            msg = gwlink.unpack(self._key, first, last_seq=last_seq, nonce=self._cn)
            last_seq = int(msg.get("seq") or 0)
            self._sn = gwlink.nonce_from(msg.get("nonce"))
            self._writer = writer
            self.online = True
            self._confirmed = True
            log.info("гостевой канал: сессия с резервом открыта (%s:%s)", host, port)
            await self._handle(msg)
            while True:
                line = await reader.readline()
                if not line:
                    break
                msg = gwlink.unpack(self._key, line, last_seq=last_seq, nonce=self._cn)
                last_seq = int(msg.get("seq") or 0)
                await self._handle(msg)
        finally:
            self.online = False

    async def _handle(self, msg: dict) -> None:
        kind = str(msg.get("t") or "")
        try:
            body = pairproto.check(pairproto.GUEST_PROTO, kind, msg, pairproto.RESERVE)
        except pairproto.ProtocolError as e:
            log.warning("гостевой канал: сообщение «%s» не разобрано: %s — сессия разорвана", kind[:16], e)
            raise ConnectionError(str(e))                     # подписанный собеседник шлёт мусор — поломка или чужой
        async with _STORE_LOCK:                              # слово резерва и такт правят reserve.json по очереди
            actions = await asyncio.to_thread(self.services.reserve_guest_in, kind, body)
        await run_actions(self.services, actions)

    async def send(self, kind: str, body: dict | None = None) -> bool:
        w = self._writer
        if w is None:
            return False
        self._seq += 1
        try:
            w.write(gwlink.pack(self._key, kind, body, seq=self._seq, pad=gwlink.PAD_DELTA, nonce=self._sn))
            await asyncio.wait_for(w.drain(), SEND_TIMEOUT)
            return True
        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            log.info("гостевой канал: «%s» не ушло: %s", kind, e)
            await self._close()
            return False


_client: GuestClient | None = None
_bot = None


def client() -> GuestClient | None:
    return _client


async def ensure(services) -> GuestClient | None:
    """Клиент — пока есть раздел reserve; без него — снять."""
    global _client
    present = await asyncio.to_thread(services.reserve_present)
    if not present:
        if _client is not None:
            await _client.stop()
            _client = None
        return None
    if _client is None:
        _client = GuestClient(services)
        _client.start()
    return _client


async def shutdown() -> None:
    global _client
    if _client is not None:
        await _client.stop()
        _client = None


async def run_actions(services, actions) -> None:
    """Действия наблюдателей и гостевого канала (домен отдаёт кортежи)."""
    for act in actions or []:
        try:
            await _one(services, act)
        except Exception as e:                            # noqa: BLE001
            log.warning("резерв (шлюз): действие %s не выполнено: %s", act[0] if act else act, e)


async def _one(services, act: tuple) -> None:
    kind = act[0]
    if kind == "guest":
        c = client()
        if c is None or not await c.send(act[1], act[2] if len(act) > 2 else None):
            log.info("гостевой канал: «%s» не ушло — сессии нет", act[1])
    elif kind == "link":
        # слово своему серверу по каналу конфигурации (кворум: qview) — литералом вида: сторож
        # видов канала (tests/unit/test_gwlink_kinds) читает отправителей
        from awgbot.runtime import linkclient
        lc = linkclient.ensure(services)
        body = dict(act[2] if len(act) > 2 else {})
        if lc is not None and getattr(lc, "_writer", None) is not None:
            if str(act[1]) == "qview":
                await lc._send("qview", body)                 # noqa: SLF001
            else:
                log.warning("канал конфигурации: слово «%s» серверу неизвестно — не ушло", act[1])
    elif kind == "open":
        from awgbot.infra.gwguard import rsv as rsvlink
        await asyncio.to_thread(rsvlink.open_forward, list(act[1]))
    elif kind == "close":
        from awgbot.infra.gwguard import rsv as rsvlink
        await asyncio.to_thread(rsvlink.close_forward)
    elif kind == "push":
        from awgbot.runtime import linkclient
        lc = linkclient.ensure(services)
        if lc is not None:
            await lc.push()
    elif kind == "notify":
        from awgbot.bot.notifier import send_notifications
        if _bot is not None:
            await send_notifications(_bot, act[1])
    elif kind == "tg":
        # обход: Telegram агента через awgrsv (метка сокетов Bot API) и обратно; сессия Bot API —
        # заново на каждом переключении: живые соединения остались бы на прежнем пути
        from awgbot.infra.gwguard import rsv as rsvlink
        await asyncio.to_thread(rsvlink.run, "tg", str(act[1]))
        await _renew_session(check=str(act[1]) == "on")
    elif kind == "reresolve":
        from awgbot.domain.gateway.reserve import uplink
        p = dict(act[1] or {})
        res = await asyncio.to_thread(uplink.reresolve_now, str(p.get("name") or ""), str(p.get("expect") or ""),
                                      str(p.get("why") or ""), int(p.get("move") or 0))
        if hasattr(services, "reserve_reresolve_done"):
            await asyncio.to_thread(services.reserve_reresolve_done, p, str(res or ""))
    elif kind == "tg_assert":
        from awgbot.infra.gwguard import rsv as rsvlink
        await asyncio.to_thread(rsvlink.run, "tg", str(act[1]))   # правило метки заново, сессия Bot API прежняя


async def _renew_session(*, check: bool) -> None:
    from awgbot.core import settings
    from awgbot.runtime import tgsession
    if _bot is None:
        return
    old = getattr(_bot, "session", None)
    _bot.session = tgsession.make(ipv4_only=settings.get_bool("app.gateway.ipv4_only", True))
    if old is not None:
        try:
            await old.close()
        except Exception:                                     # noqa: BLE001
            pass
    if check:
        try:
            await asyncio.wait_for(_bot.get_me(), timeout=10)
        except Exception as e:                                # noqa: BLE001
            log.warning("обход: через awgrsv Telegram не ответил за 10 с (%s) — обход остаётся, другого пути нет",
                        type(e).__name__)


async def tick(services, bot=None) -> None:
    """Такт 5 с из планировщика агента: клиент гостевого канала, наблюдатели, действия."""
    global _bot
    if bot is not None:
        _bot = bot
    c = await ensure(services)
    async with _STORE_LOCK:
        actions = await asyncio.to_thread(services.reserve_watch_tick)
    if c is not None and not c.online and any(a[0] == "guest" for a in actions):
        c.wake()
    await run_actions(services, actions)
    if c is not None:
        services.__dict__["_quo_reserve_online"] = bool(c.online)


_quo_q: tuple[object, asyncio.Lock] | None = None   # (петля, замок очереди слов кворума)
_quo_tasks: set = set()


def _quo_lock() -> asyncio.Lock:
    global _quo_q
    loop = asyncio.get_running_loop()
    if _quo_q is None or _quo_q[0] is not loop:
        _quo_q = (loop, asyncio.Lock())
    return _quo_q[1]


async def _quo_send(services, acts: list) -> None:
    async with _quo_lock():                                  # слова кворума — в порядке смены взгляда
        await run_actions(services, acts)


def _quo_spawn(services, acts) -> None:
    """Слова кворума — фоном: отправка по каналу ждёт до SEND_TIMEOUT, проба — нет."""
    t = asyncio.get_running_loop().create_task(_quo_send(services, list(acts)))
    _quo_tasks.add(t)
    t.add_done_callback(_quo_tasks.discard)


async def quorum_tick(services, bot=None) -> None:
    """Такт кворума из планировщика агента (своё задание): зонды в awg-quo, пробы и
    взгляд голосующего (runtime/quorum); слова — фоном по очереди."""
    global _bot
    if bot is not None:
        _bot = bot
    c = client()
    services.__dict__["_quo_reserve_online"] = bool(c is not None and c.online)
    try:
        from awgbot.runtime import quorum as _quorum

        async def _act(acts) -> None:
            _quo_spawn(services, acts)
        await _quorum.tick(services, _act)
    except Exception as e:                                # noqa: BLE001
        log.warning("кворум (шлюз): такт: %s", e)
