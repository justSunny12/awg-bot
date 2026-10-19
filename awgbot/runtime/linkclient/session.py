"""session.py — подключение к ВПС: сокет, hello, ключ канала, переподключение с задержкой, запуск и остановка."""
from __future__ import annotations

import asyncio
import contextlib
import random
import time

from awgbot.core import config
from awgbot.util import gwlink
from awgbot.runtime import linkclient as pkg          # адрес, порт, крючок и задержки — на пакете: их подменяют тесты

from .common import CAPS, log


class SessionMixin:
    def __init__(self, services):
        self.services = services
        self._task: asyncio.Task | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._key: bytes | None = None
        self._seq = 0
        self._rev = 0
        self._prev: dict = {}
        self._sent_bytes = 0                  # для тестов «в простое ноль пакетов»
        self._parts: dict = {}                # сборка фидов, пришедших частями
        self._confirmed = False               # сервер ответил в этой сессии хоть раз
        self._link_ok: bool | None = None     # линк на прошлом тике — ловим восстановление
        self._cn = b""                        # наш нонс: им сервер подписывает нам
        self._sn = b""                        # нонс сервера: им подписываем мы
        self._wake = asyncio.Event()          # будит бэкофф: линк восстановился — подключаться сразу
        # применение канона своих списков и сверка файлов не пересекаются:
        # сверка посреди `sync` сочла бы правки канона своими
        self._own_lock = asyncio.Lock()
        self._push_lock = asyncio.Lock()
        self._fill_task: asyncio.Task | None = None   # наполнение набора после sync — фоном

    # ── соединение ───────────────────────────────────────────────────────────

    def _channel_key(self) -> bytes:
        if self._key is None:
            from awgbot.util import bundlecrypt
            with open(config.GW_LINK_CONF, encoding="utf-8") as f:
                self._key = gwlink.channel_key(bundlecrypt.read_privkey(f.read()))
        return self._key

    def _io(self, rx: int = 0, tx: int = 0) -> None:
        """Байты канала поверх счётчиков линка (services.channel, слот 0) — зонд
        выхода наружу по уликам вычитает их: ответ канала серверу не должен
        сходить за ответы из интернета, ушедшие клиентам."""
        self.services.channel.account(0, rx=rx, tx=tx)

    async def _connect_once(self) -> None:
        host, port = pkg.server_address(), pkg.server_port()
        if not host:
            raise OSError("адрес ВПС в линке не определён")
        # Предел строки — тот же MAX_LINE, что у разбора: большое тело сервер
        # режет на части (gwlink.CHUNK), и ни одна из них его не перерастает.
        # Ключ — заново на каждое подключение: бандл из чата мог привезти новый
        # ключ линка, а процесс агента тот же.
        self._key = None
        # Коннект с таймаутом: без него попытка при дропе SYN висела бы до
        # системного, минуты, и бэкофф считал бы не то.
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, limit=gwlink.MAX_LINE), timeout=20)
        # keepalive ядра: полуоткрытая сессия после жёсткого ребута ВПС иначе
        # висит днями — переподключения ждать не от чего
        gwlink.tcp_keepalive(writer)
        self._seq = 0
        self._rev = 0
        self._prev = {}
        self._parts = {}                      # части фидов прошлой сессии — не наши
        self._confirmed = False
        self._cn, self._sn = gwlink.new_nonce(), b""
        log.info("канал линка: подключился к %s:%s", host, port)
        try:
            # hello — без нонса (сервер свой ещё не назвал), с нашим нонсом внутри.
            # _writer до ответа сервера не выставляем: снимок из тика или poke
            # ушёл бы без нонса сервера, и тот оборвал бы сессию.
            lists_hash = await asyncio.to_thread(self.services.lan_feeds_applied_hash)
            # отпечаток применённых записей соседей — сервер не повезёт то же самое
            svc_hash = await asyncio.to_thread(self.services.services_applied_hash)
            self._seq += 1
            hello_body = {"proto": gwlink.PROTO, "agent": config.INSTALLED_VERSION,
                          "lists_hash": lists_hash, "svc_hash": svc_hash,
                          "nonce": gwlink.nonce_b64(self._cn)}
            # отпечаток применённого канона своих списков: само поле говорит
            # серверу, что агент синхронизацию знает (без него канон не шлётся)
            hello_body["own_hash"] = await asyncio.to_thread(self.services.own_applied_hash)
            hello_body["caps"] = list(CAPS)             # контракт: возможности, не версия
            # копия настроек шлюза: отпечаток и чистота установки — сервер решает,
            # вернуть ли блок; своё агент шлёт только после его ответа
            joining = await asyncio.to_thread(self.services.join_hello) if hasattr(self.services, "join_hello") else {}
            pairing = bool(getattr(self.services, "join_active", None) and
                           await asyncio.to_thread(self.services.join_active))
            if joining:
                # сессия сопряжения: сервер в ней разбирает только своё (join_*, link_ack);
                # копией настроек не обмениваемся (как и ниже, до полного старта)
                hello_body.update(joining)
                hello_body["caps"] = [c for c in CAPS if c != "keep1"]
            elif pairing:
                # после кода, до полного старта агента: копией настроек не
                # обмениваемся — установка ещё не «новая» (отметки первого старта
                # нет), и умолчания затёрли бы блок слота на сервере; обмен — в
                # первой сессии полного агента
                hello_body["caps"] = [c for c in CAPS if c != "keep1"]
            elif hasattr(self.services, "keep_hello"):
                hello_body.update(await asyncio.to_thread(self.services.keep_hello))
            self._keep_ready = False
            hello = gwlink.pack(self._channel_key(), "hello", hello_body,
                                seq=self._seq, pad=gwlink.PAD_DELTA)
            writer.write(hello)
            await writer.drain()
            self._sent_bytes += len(hello)
            self._io(tx=len(hello))
            # первое сообщение сервера — подписано нашим нонсом и несёт его нонс
            first = await asyncio.wait_for(reader.readline(), timeout=20)
            if not first:
                return
            self._io(rx=len(first))
            msg = gwlink.unpack(self._channel_key(), first, nonce=self._cn)
            self._sn = gwlink.nonce_from(msg.get("nonce"))
            last_seq: int | None = int(msg.get("seq") or 0)
            self._confirmed = True
            self.services.channel.last_word = time.time()
            # сервер ответил подписанным словом — агент на связи: отметка для
            # сторожа обновления (одна на процесс, только когда он взведён)
            from awgbot.domain.selfupdate.marks import mark_alive
            mark_alive("сервер AWG ответил в канале")
            self._writer = writer
            self._set_online(True)
            await self._handle(msg)
            if joining and str(msg.get("t") or "").startswith("join_"):
                await self._join_loop(reader, last_seq)
                return
            # hello с join, а сервер ответил обычной сессией: ВПС уже на
            # постоянных ключах — дальше как обычно (фиксация смены ключей)
            # ответ на документ — первым: сервер узнаёт итог раньше, чем снимок с прежними
            # отпечатками после отката, и не повторяет доставку вслепую
            await self.flush_cfg_ack()
            await self.flush_upd_ack()          # итог команды обновления — прежде снимка
            self.__dict__["_upd_resume_task"] = asyncio.get_running_loop().create_task(self._upd_resume())
            await self._rekey_answered()        # ответ на новых или прежних ключах закрепляет выбор
            await self.push(full=True)
            await self.push_services()
            await self._maybe_installed()
            await self._maybe_join_done()
            await self.push_own()
            await self._maybe_claim()
            while True:
                try:
                    line = await reader.readline()
                except ValueError:
                    log.warning("канал линка: строка с ВПС сверх предела — разрыв")
                    break
                if not line:
                    break
                self._io(rx=len(line))
                try:
                    msg = gwlink.unpack(self._channel_key(), line, last_seq=last_seq, nonce=self._cn)
                except gwlink.ProtocolError as e:
                    log.warning("канал линка: сообщение с ВПС отвергнуто — %s", e)
                    break
                last_seq = int(msg.get("seq") or 0)
                self.services.channel.last_word = time.time()   # слово сервера: сессия свежая
                await self._handle(msg)
        finally:
            self._writer = None
            self._set_online(False)
            self._rst_broken()                  # передача бэкапа серверу посреди — оборвалась
            # Запас на своё скачивание фидов отсчитывается от конца сессии — но
            # только настоящей: отвергнутый коннект (старый ключ, чужой слот)
            # продлевал бы запас каждым стуком, и фиды застывали навсегда.
            if self._confirmed:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.services.lan_feeds_touch)
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    def _set_online(self, on: bool) -> None:
        """Признак «сессия открыта» — для домена агента, без импорта runtime."""
        self.services.channel.online = on

    async def _run(self) -> None:
        attempt = 0
        while True:
            if self._join_dead():
                log.info("канал линка: сопряжение закрыто сервером AWG — не подключаюсь")
                return
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:             # noqa: BLE001
                # Молча: линк и так даёт свой алерт, а второй про то же — шум.
                log.info("канал линка: нет связи с ВПС (%s)", e)
            # Счёт заново — только если сервер ответил хоть одним подписанным
            # сообщением. Сессия, которую сервер закрыл сразу (чужая подпись,
            # чужой слот), иначе превращалась в стук каждые пять секунд без
            # роста шага: ровный ритм в линке и запись на SD на каждом коннекте.
            if self._confirmed:
                attempt = 0
                self._confirmed = False
            delay = pkg._BACKOFF[min(attempt, len(pkg._BACKOFF) - 1)] * random.uniform(0.6, 1.4)
            attempt += 1
            # Спим с побудкой: восстановление линка (on_tick) будит сразу и
            # обнуляет шаг — иначе канал поднимался до 7 минут после возврата ВПС.
            if await self._sleep_or_wake(delay):
                attempt = 0

    async def _sleep_or_wake(self, delay: float) -> bool:
        """Спать delay секунд или до побудки; True — разбудили раньше срока."""
        self._wake.clear()
        sleeper = asyncio.ensure_future(asyncio.sleep(delay))
        waker = asyncio.ensure_future(self._wake.wait())
        try:
            done, _pending = await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (sleeper, waker):
                if not t.done():
                    t.cancel()
        if sleeper in done:
            sleeper.result()                  # исключение сна — наружу, как у обычного sleep
        return waker in done
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
        if self._fill_task is not None and not self._fill_task.done():
            # поток с dig не прервать; задача-обёртка отпускается, итог — в журнал
            self._fill_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._fill_task
        self._fill_task = None
