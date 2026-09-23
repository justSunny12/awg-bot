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

С ВПС по той же сессии приходят только данные из закрытого списка (этапы 2–4):
`settings` — четыре настройки обвязки (применяет `apply_link_settings`, ответ
`ack`, уведомление человеку в чат агента), `lists` — фиды локальной сети
(`apply_lan_feeds`, ответ `lists_ack`; `lists_ok` — у шлюза уже те же,
запас своего скачивания отсчитывается от конца сессии), `role` — несёт ли слот трафик, и
`ask`/`tail` — одна из трёх диагностик только на чтение. Неизвестный вид
пропускается с записью в журнал: новый ВПС со старым агентом не рвёт сессию.
Окно ±5 минут (`gwlink.MAX_SKEW_SECONDS`) — по часам этого хоста: их
синхронизация здесь требование, а не пожелание.
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
        self._parts: dict = {}                # сборка фидов, пришедших частями

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
        # Фиды локальной сети (этап 3) едут одной строкой: сжатые они весят
        # килобайты — до предела MAX_LINE запас в десятки раз.
        reader, writer = await asyncio.open_connection(host, port, limit=gwlink.MAX_LINE)
        self._writer = writer
        self._seq = 0
        self._rev = 0
        self._prev = {}
        log.info("канал линка: подключился к %s:%s", host, port)
        try:
            lists_hash = await asyncio.to_thread(self.services.lan_feeds_hash)
            await self._send("hello", {"proto": gwlink.PROTO,
                                       "agent": config.INSTALLED_VERSION,
                                       "lists_hash": lists_hash})
            await self.push(full=True)
            await self._maybe_claim()
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
            # запас на своё скачивание фидов отсчитывается от конца сессии
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.services.lan_feeds_touch)
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
        отпечаток применённого — сервер по нему понимает, что доставка
        завершена, и больше этот набор не шлёт."""
        result = await asyncio.to_thread(self.services.apply_link_settings, values or {})
        body = {"ok": bool(result.get("ok")), "changed": list(result.get("changed") or []),
                "error": str(result.get("error") or "")[:300]}
        try:
            body["hash"] = gwlink.settings_hash(gwlink.validate_settings(values or {}))
        except gwlink.ProtocolError:
            body["hash"] = ""
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
        if msg.get("t") == "ask" and msg.get("what") == "tail":
            name = str(msg.get("name") or "")
            text = await asyncio.to_thread(self.services.diag_tail, name)
            await self._send("tail", {"name": name, "text": text}, pad=gwlink.PAD_DELTA)
            return
        if msg.get("t") == "lists_ok":
            # сервер при подключении подтвердил: у нас те же фиды, что у него
            await asyncio.to_thread(self.services.lan_feeds_touch, str(msg.get("hash") or ""))
            return
        if msg.get("t") == "lists":
            await self._apply_lists(str(msg.get("hash") or "")[:64], str(msg.get("z") or ""))
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
    """Поднять клиента, если канал включён бандлом. Роль client сюда не ходит."""
    global _client
    if config.ROLE != "gateway" or not enabled():
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
    своего расписания у канала нет по замыслу."""
    client = ensure(services)
    if client is not None:
        await client.push()


async def shutdown() -> None:
    global _client
    if _client is not None:
        await _client.stop()
        _client = None
