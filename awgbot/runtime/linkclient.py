"""
linkclient.py — сторона шлюза для канала ВПС ↔ шлюз (концепт «канал линка»).

Коннект устанавливает ТОЛЬКО малина: на ней не появляется ни одного слушающего
сокета, и механика работает за любым NAT. Ответные пакеты сервера проходят в
`tunnel_in` по `ct state established` — на шлюзе не нужно ни одного нового
правила файервола.

Периодической задачи здесь нет. Сессия держится открытой, и в простое по ней не
уходит ни байта: снимок отправляется при подключении, дальше — только когда
факт или вердикт действительно сменились. Переподключение — с бэкоффом и
джиттером: ровный ритм попыток был бы тем же маячком, только на уровне TCP.
Бэкофф сбрасывает только сессия, в которой сервер ответил. Линк лёг и поднялся
(`on_tick`) — сессия рвётся и ставится заново: после жёсткого ребута ВПС она
полуоткрыта, а в простое агент сам ничего не шлёт. После операций из чата
(бандл, мастер восстановления) снимок уходит сразу (`poke`). Байты канала
копятся в `services.channel`: зонд выхода наружу вычитает их из счётчиков линка.

Канал включается тем, что привёз бандл (`LINK_CHANNEL=1` в юните обвязки): без
перевыпуска конфигурации агент никуда не ходит — это и есть рубильник.

С ВПС по той же сессии приходят только данные из закрытого списка (этапы 2–4):
`settings` — четыре настройки обвязки (применяет `apply_link_settings`, ответ
`ack`, уведомление человеку в чат агента), `lists` — фиды локальной сети
(`apply_lan_feeds`, ответ `lists_ack`; `lists_ok` — у шлюза уже те же,
запас своего скачивания отсчитывается от конца сессии), `role` — несёт ли слот
трафик, `ask snap` — просьба о полном снимке. Неизвестный вид
пропускается с записью в журнал: новый ВПС со старым агентом не рвёт сессию.
Повтор отсекают нонсы сессии (gwlink, proto 2): hello несёт наш нонс, первое
сообщение сервера — его; дальше подписываем нонсом сервера, проверяем своим.
Часы хоста в проверку не входят — малина без RTC поднимает канал и до NTP.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import random
import subprocess

from awgbot.core import config
from awgbot.domain import gwservices, gwsnapshot
from awgbot.util import gwlink

log = logging.getLogger(__name__)

DEFAULT_PORT = gwlink.DEFAULT_PORT
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
        self._parts: dict = {}                # сборка фидов, пришедших частями
        self._confirmed = False               # сервер ответил в этой сессии хоть раз
        self._link_ok: bool | None = None     # линк на прошлом тике — ловим восстановление
        self._cn = b""                        # наш нонс: им сервер подписывает нам
        self._sn = b""                        # нонс сервера: им подписываем мы

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
        host, port = server_address(), server_port()
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
        self._seq = 0
        self._rev = 0
        self._prev = {}
        self._confirmed = False
        self._cn, self._sn = gwlink.new_nonce(), b""
        log.info("канал линка: подключился к %s:%s", host, port)
        try:
            # hello — без нонса (сервер свой ещё не назвал), с нашим нонсом внутри.
            # _writer до ответа сервера не выставляем: снимок из тика или poke
            # ушёл бы без нонса сервера, и тот оборвал бы сессию.
            lists_hash = await asyncio.to_thread(self.services.lan_feeds_applied_hash)
            # отпечаток применённых записей соседей — сервер не повезёт то же самое
            svc_hash = await asyncio.to_thread(
                getattr(self.services, "services_applied_hash", lambda: ""))
            self._seq += 1
            self._svc_sent = None
            hello = gwlink.pack(self._channel_key(), "hello",
                                {"proto": gwlink.PROTO, "agent": config.INSTALLED_VERSION,
                                 "lists_hash": lists_hash, "svc_hash": svc_hash,
                                 "nonce": gwlink.nonce_b64(self._cn)},
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
            self._writer = writer
            self._set_online(True)
            await self._handle(msg)
            await self.push(full=True)
            await self.push_services(force=True)
            await self.flush_applied()
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
                await self._handle(msg)
        finally:
            self._writer = None
            self._set_online(False)
            # запас на своё скачивание фидов отсчитывается от конца сессии
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
            delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)] * random.uniform(0.6, 1.4)
            attempt += 1
            await asyncio.sleep(delay)

    # ── отправка ─────────────────────────────────────────────────────────────

    async def _send(self, kind: str, body: dict | None = None, pad: int = 0) -> bool:
        writer = self._writer
        if writer is None:
            return False
        self._seq += 1
        line = gwlink.pack(self._channel_key(), kind, body, seq=self._seq, pad=pad, nonce=self._sn)
        try:
            writer.write(line)
            await writer.drain()
            self._sent_bytes += len(line)
            self._io(tx=len(line))
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
        sent = await self._send("delta", {**patch, "rev": self._rev}, pad=gwlink.PAD_DELTA)
        if sent and "mark_status" in patch:
            # Пометка сменилась (применили бандл нового слота) — не заставляем
            # человека пересылать токен руками, если канал уже жив.
            await self._maybe_claim()
        return sent

    async def _apply_settings(self, values) -> None:
        """Настройки с сервера: проверить, применить, ответить, сказать человеку.

        Применение — в потоке: рестарт юнита обвязки идёт секунды, и держать на
        это время цикл событий агента (его Telegram) незачем. Ответ `ack` несёт
        итог применения; что доставка завершена, сервер видит
        завершена по снимку, который уходит следом."""
        result = await asyncio.to_thread(self.services.apply_link_settings, values or {})
        body = {"ok": bool(result.get("ok")), "changed": list(result.get("changed") or []),
                "error": str(result.get("error") or "")[:300]}
        await self._send("ack", body, pad=gwlink.PAD_DELTA)
        if result.get("changed") or not result.get("ok"):
            # Человек узнаёт о факте в чате агента: применено без его кнопки,
            # значит сказать обязательно. Пустое применение — тишина.
            if _notify is not None:
                try:
                    await _notify(self.services.link_settings_note(result))
                except Exception as e:                    # noqa: BLE001
                    log.info("канал линка: уведомление не ушло: %s", e)
            await self.push()                             # снимок с новыми значениями — сразу

    def _collect_part(self, msg: dict) -> str | None:
        """Сложить часть фидов; вернуть целое, когда пришли все. Части другого
        отпечатка сбрасывают начатую сборку: сервер сменил фиды на ходу."""
        digest = str(msg.get("hash") or "")[:64]
        try:
            i, n = int(msg.get("i")), int(msg.get("n"))
        except (TypeError, ValueError):
            return None
        if not (0 < n <= gwlink.MAX_CHUNKS and 0 <= i < n):
            return None
        if self._parts.get("hash") != digest or self._parts.get("n") != n:
            self._parts = {"hash": digest, "n": n, "got": {}}
        self._parts["got"][i] = str(msg.get("z") or "")[:gwlink.CHUNK]
        if len(self._parts["got"]) < n:
            return None
        whole = "".join(self._parts["got"][k] for k in range(n))
        self._parts = {}
        return whole

    async def flush_applied(self) -> bool:
        """Отправить серверу отложенный итог применения файла. Из очереди он
        уходит по подтверждению сервера (applied_ack), а не по записи в сокет:
        бандл тут же перезапускает линк, и строка в буфере умершей сессии
        пропала бы вместе с ней. Повтор сервер отличает по отпечатку и времени."""
        if self._writer is None:
            return False
        get = getattr(self.services, "applied_pending_get", None)
        if get is None:
            return False
        pending = await asyncio.to_thread(get)
        if not pending:
            return False
        return await self._send("applied", {"ok": bool(pending.get("ok")),
                                            "error": str(pending.get("error") or "")[:300],
                                            "fp": str(pending.get("fp") or "")[:32],
                                            "at": str(pending.get("at") or "")[:40]},
                                pad=gwlink.PAD_DELTA)

    async def _applied_acked(self, msg: dict) -> None:
        """Сервер принял итог: очередь пуста. Подтверждение на другой файл
        (очередь успели перезаписать) очередь не трогает."""
        get = getattr(self.services, "applied_pending_get", None)
        if get is None:
            return
        pending = await asyncio.to_thread(get)
        if not pending:
            return
        if str(pending.get("fp") or "") != str(msg.get("fp") or "") or \
                str(pending.get("at") or "") != str(msg.get("at") or ""):
            return
        await asyncio.to_thread(self.services.applied_pending_clear)

    async def push_services(self, *, force: bool = False) -> bool:
        """Сервисы этой сети — серверу (концепт «сервисы соседних сетей»): при
        подключении целиком, дальше только при изменении своего списка."""
        if self._writer is None:
            return False
        get = getattr(self.services, "services_local", None)
        if get is None:
            return False
        items = await asyncio.to_thread(get)
        digest = gwservices.feed_hash(items)
        if not force and digest == self._svc_sent:
            return False
        self._svc_sent = digest
        return await self._send("svc", {"items": items}, pad=gwlink.PAD_SNAP)

    async def _apply_peer_services(self, msg: dict) -> None:
        apply = getattr(self.services, "apply_peer_services", None)
        if apply is None:
            return
        digest = str(msg.get("hash") or "")[:64]
        items = msg.get("items") if isinstance(msg.get("items"), list) else []
        result = await asyncio.to_thread(apply, digest, items[:gwservices.MAX_PEER * 2])
        await self._send("peer_svc_ack", {"ok": bool(result.get("ok")), "hash": digest,
                                          "n": int(result.get("n") or 0),
                                          "error": str(result.get("error") or "")[:300]},
                         pad=gwlink.PAD_DELTA)

    async def retry_peer_services(self) -> bool:
        """Отложенное применение записей соседей (помощника не было): помощник
        появился — применить и отчитаться серверу тем же отпечатком."""
        retry = getattr(self.services, "services_retry", None)
        if retry is None or self._writer is None:
            return False
        result = await asyncio.to_thread(retry)
        if not result:
            return False
        return await self._send("peer_svc_ack", {"ok": bool(result.get("ok")),
                                                 "hash": str(result.get("hash") or "")[:64],
                                                 "n": int(result.get("n") or 0),
                                                 "error": str(result.get("error") or "")[:300]},
                                pad=gwlink.PAD_DELTA)

    async def _apply_lists(self, digest: str, z: str) -> None:
        result = await asyncio.to_thread(self.services.apply_lan_feeds, digest, z)
        await self._send("lists_ack", {"ok": bool(result.get("ok")), "hash": digest,
                                       "error": str(result.get("error") or "")[:300]},
                         pad=gwlink.PAD_DELTA)

    async def _maybe_claim(self) -> None:
        """Шлюз в основном боте не помечен — отправить токен пометки каналом.
        Проверка на той стороне — общая с ручной пересылкой, включая нонсы."""
        try:
            token = await asyncio.to_thread(self.services.gateway_claim_if_needed)
        except Exception as e:                            # noqa: BLE001
            log.info("канал линка: claim не собран: %s", e)
            return
        if token:
            await self._send("claim", {"token": token}, pad=gwlink.PAD_SNAP)

    async def _handle(self, msg: dict) -> None:
        """Разбор одного сообщения. Исключение внутри — в журнал, а не наружу:
        иначе поломка одного обработчика рвала бы сессию целиком, и та же
        доставка повторялась бы на каждом переподключении."""
        try:
            await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:                            # noqa: BLE001
            log.warning("канал линка: сообщение «%s» не обработано: %s", msg.get("t"), e)

    async def _dispatch(self, msg: dict) -> None:
        if msg.get("t") == "lists_part":
            whole = self._collect_part(msg)
            if whole is not None:
                await self._apply_lists(str(msg.get("hash") or "")[:64], whole)
            return
        if msg.get("t") == "ask" and msg.get("what") == "snap":
            await self.push(full=True)
            return
        if msg.get("t") == "settings":
            await self._apply_settings(msg.get("values"))
            return
        if msg.get("t") == "role":
            await asyncio.to_thread(self.services.set_link_role, bool(msg.get("active")))
            return
        if msg.get("t") == "lists_ok":
            # сервер при подключении подтвердил: у нас те же фиды, что у него
            await asyncio.to_thread(self.services.lan_feeds_touch, str(msg.get("hash") or ""))
            return
        if msg.get("t") == "lists":
            await self._apply_lists(str(msg.get("hash") or "")[:64], str(msg.get("z") or ""))
            return
        if msg.get("t") == "peer_svc":
            await self._apply_peer_services(msg)
            return
        if msg.get("t") == "applied_ack":
            await self._applied_acked(msg)
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
_notify = None


def set_notify(fn) -> None:
    """Корутина `fn(text)` — отправить человеку в чат агента. Ставит main:
    у клиента канала своего бота нет."""
    global _notify
    _notify = fn


def ensure(services) -> LinkClient | None:
    """Поднять клиента, если канал включён бандлом. Роль client сюда не ходит.
    Бандл с LINK_CHANNEL=0 гасит уже запущенного клиента на ближайшем тике —
    рубильник не должен ждать перезапуска агента."""
    global _client
    if config.ROLE != "gateway":
        return None
    if not enabled():
        if _client is not None and _client._task is not None:
            _client._task.cancel()
            _client._task = None
            _client._set_online(False)
        return None
    if _client is None:
        _client = LinkClient(services)
    _client.start()
    return _client


def role() -> str:
    """active | standby | "" — что последним сообщил сервер."""
    if _client is None:
        return ""
    try:
        return _client.services.link_role()
    except Exception:                                     # noqa: BLE001
        return ""


def online() -> bool:
    """Сессия с ВПС сейчас открыта. Без сети: просто состояние объекта."""
    return _client is not None and _client._writer is not None


async def on_tick(services) -> None:
    """После тика монитора: отправить дельту, если что-то действительно
    изменилось. Зовётся из задачи планировщика, а не своим расписанием —
    своего расписания у канала нет по замыслу.

    Здесь же — переподключение по восстановлению линка. Жёсткий ребут ВПС не
    шлёт FIN, а агент в простое сам ничего не отправляет и RST не получит:
    сессия висела бы «открытой» днями, подавляя своё скачивание фидов. Линк
    лёг и поднялся — значит, прежней сессии на той стороне нет наверняка;
    рвём и подключаемся заново. Ноль пакетов сверх того, что и так идёт."""
    client = ensure(services)
    if client is None:
        return
    try:
        st = services.cached_status(24 * 3600)
        link_ok = bool(st is not None and services.link_ok(st))
    except Exception:                                     # noqa: BLE001
        link_ok = None
    if link_ok is not None:
        was = client._link_ok
        client._link_ok = link_ok
        if was is False and link_ok and client._writer is not None:
            log.info("канал линка: линк восстановился — переподключаюсь")
            with contextlib.suppress(Exception):
                client._writer.close()
            return
    await client.push()
    with contextlib.suppress(Exception):
        await client.retry_peer_services()


async def poke(services) -> None:
    """После операции из чата агента (бандл, мастер восстановления): поднять
    клиента, если бандл только что включил канал, и сразу отправить дельту —
    человек ждёт результата на ВПС сейчас, а не через тик. Заодно — внеочередной
    обзор сервисов: бандл мог включить или выключить доступ между подсетями."""
    client = ensure(services)
    if client is not None:
        with contextlib.suppress(Exception):
            await client.push()
    with contextlib.suppress(Exception):
        await services_changed(services)


async def report_applied(services, ok: bool, error: str = "", fp: str = "") -> None:
    """Файл конфигурации применён из чата агента (или нет): сказать серверу
    сразу, если канал жив, иначе — первым делом при подключении. fp —
    отпечаток файла (sha256 шифрованного, первые 16 знаков): сервер сверит
    его с выданным и не уберёт из чата чужой или более новый файл."""
    setter = getattr(services, "applied_pending_set", None)
    if setter is None:
        return
    await asyncio.to_thread(setter, ok, error, fp)
    client = _client
    if client is not None and client._writer is not None:
        with contextlib.suppress(Exception):
            await client.flush_applied()


async def services_changed(services) -> None:
    """Задача обзора (концепт «сервисы соседних сетей»): изменился свой список
    — отправить серверу, если канал жив; нет — уйдёт целиком при подключении."""
    scan = getattr(services, "services_scan", None)
    if scan is None:
        return
    changed = await asyncio.to_thread(scan)
    client = _client
    if changed and client is not None and client._writer is not None:
        await client.push_services(force=True)


async def shutdown() -> None:
    global _client
    if _client is not None:
        await _client.stop()
        _client = None
