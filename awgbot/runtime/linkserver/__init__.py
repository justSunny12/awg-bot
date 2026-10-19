"""
linkserver/ — сторона ВПС для канала ВПС ↔ шлюз (части: common — сессия и предел hello;
session — слушатели и сессии; inbound — разбор входящих; deliver — доставки; rekey — смена ключей линка: link_next,
link_go, переключение ВПС, фиксация первым шагом hello, сроки; relay — бэкап сервера из чата бота шлюза;
upd — набор обновлений: команды шлюзам, их ответы, такт набора).

Слушатель живёт ТОЛЬКО на адресах линков: публичный адрес сервера порт канала
не показывает даже при снятом файерволе. Коннект всегда устанавливает шлюз —
на шлюзе не появляется ни одного слушающего сокета, и механика одинаково
работает за любым NAT.

По серверу на адрес линка, а не один на все: bind списка адресов в asyncio —
«всё или ничего», и лежащий второй линк оставлял бы без канала здоровый
первый. Отдельные серверы изолируют отказ по слотам и не рвут чужие сессии,
когда состав слотов меняется.

Слот определяется парой «на какой локальный адрес пришли» и «каким ключом линка
проверилась подпись»: обе должны указывать на один слот, иначе разрыв. Вторая
сессия того же слота вытесняет первую — шлюз перезагрузился, старая висит.
Повтор отсекают нонсы сессии (gwlink, proto 2): hello приносит нонс шлюза
(поле `nonce`), первое сообщение сервера несёт его нонс тем же полем, дальше
каждая сторона подписывает нонсом получателя. Часы в проверку не входят. До
hello сервер не шлёт ничего.
Ключ читается заново на каждую сессию: после замены устройства в слоте ключ линка
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
(`monitor_tick`) — отдельной задачей `deliver_soon`, такт её не ждёт; правка
из чата основного бота будит её сразу (`touch`, он же `services.channel_touch`:
сервисы работают в потоке) — сверяется локально, что разошлось,
и по сети уходит только это — документ конфигурации (`deliver` →
`deliver_cfg`), фиды локальной сети
(`deliver_lists`), записи SMB соседних сетей (`deliver_peer_services`), канон
своих списков (`deliver_own`), роль слота (`send_role`: активен ли, есть ли
другой слот с устройством, имя слота, возможности и версия сервера, адреса в
туннеле сети слота; её такт говорит каждый раз), смена ключей линка
(`deliver_rekey`: сначала отзыв `link_cancel`, ждущий сессии после возврата
ВПС на прежние ключи, затем `link_next`). Один и тот же документ за сессию
повторно не уходит — кроме временного отказа агента (`cfg_ack` с `retry`):
тогда документ снова ждёт расхождения снимка. Полный снимок сервер просит сам (`ask snap`) —
только при разрыве нумерации дельт; по запросу человека ничего у шлюза не
спрашивается, диагностики по каналу нет.

"""
from __future__ import annotations

import asyncio
import logging

from awgbot.core import config
from awgbot.util import gwlink

from .common import HELLO_TIMEOUT, _Session
from .session import SessionMixin
from .inbound import InboundMixin
from .deliver import DeliverMixin
from .rekey import RekeyMixin
from .relay import RelayMixin
from .join import JoinMixin

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



class LinkServer(SessionMixin, InboundMixin, DeliverMixin, RekeyMixin, RelayMixin, JoinMixin):
    """По серверу на адрес линка, по сессии на слот — собирается из частей пакета."""


# ── единственный экземпляр на процесс ────────────────────────────────────────

_server: LinkServer | None = None


_on_installed = None
_on_restore = None
_on_join = None


def set_on_join(fn) -> None:
    """Сообщение подключения шлюза (async fn(event, st, extra)): шлюз
    подключился, заново, с двух адресов, срок вышел. Ставит main."""
    global _on_join
    _on_join = fn


async def join_accepted(slot: int) -> None:
    """Код верный (из чата): секреты и новые ключи — живой сессии сопряжения."""
    if _server is not None:
        await _server.join_accepted(int(slot))


async def join_wrong(slot: int, left: int) -> None:
    if _server is not None:
        await _server.join_wrong(int(slot), int(left))


async def join_close(services, slot: int, why: str) -> str:
    """Отказ шлюзу и интерфейс под токен прочь — сразу (Р24). "" — сделано,
    иначе причина (возврат повторит такт)."""
    if _server is not None:
        return await _server.join_close(int(slot), why)
    return await asyncio.to_thread(services.gwjoin_revert, int(slot), why) or ""
_on_notes = None
_on_rekeyed = None
def set_on_rekeyed(fn) -> None:
    """Ключи линка слота сменены (async fn(slot_id)): прежний файл конфигурации
    слота в чате админа больше не годится — его убирает main."""
    global _on_rekeyed
    _on_rekeyed = fn


def set_on_notes(fn) -> None:
    """Уведомления админу о событиях канала (смена ключей линка): async fn(notes)."""
    global _on_notes
    _on_notes = fn


def set_on_restore(fn) -> None:
    """Бэкап сервера из чата бота шлюза собран (async fn(slot_id, blob, name) ->
    «» — принят и вопрос задан в чате бота сервера, иначе причина отказа для
    чата бота шлюза). Ставит main: у слушателя канала своего бота нет."""
    global _on_restore
    _on_restore = fn


def set_on_installed(fn) -> None:
    """Корутина `fn(slot_id)` — сказать админу, что новый шлюз настроен и на
    связи (вид `installed` от агента). Ставит main: у слушателя канала своего
    бота нет."""
    global _on_installed
    _on_installed = fn


async def ensure(services) -> LinkServer | None:
    """Поднять/перевесить слушатели. Роль gateway сюда не заходит вовсе.
    Сессии прошлого процесса сбрасывает старт бота (main, до поллинга)."""
    global _server
    if config.ROLE == "gateway":
        return None
    if _server is None:
        _server = LinkServer(services)
    _server._loop = asyncio.get_running_loop()
    # до слушателей: посреди переключения ключей сервер не знает, что стало со
    # шлюзом, — прежние ключи на место (шлюз найдёт их поиском)
    await _server.rekey_boot()
    await _server.ensure()
    await _server.deliver_all()
    return _server


async def join_job(services) -> None:
    """Своё задание сопряжений (30 с с джиттером, без условия маршрутизации):
    пока команда подключения жива — слушатель на адресе линка и срок (Т-П11,
    интерфейс под токен прочь сразу); сопряжений нет — выход сразу."""
    global _server
    if config.ROLE == "gateway" or not hasattr(services, "gwjoin_all"):
        return
    if not await asyncio.to_thread(services.gwjoin_all):
        return
    if _server is None:
        _server = LinkServer(services)
        _server._loop = asyncio.get_running_loop()
    await _server.ensure()
    await _server.join_tick()


async def liveness_tick(services) -> None:
    """Такт живости (30 с): слушатели по слотам, мёртвые сессии долой, роль —
    и только каждый четвёртый такт полная сверка доставки в отдельной задаче:
    ~20 запросов к БД на сессию каждые 30 с ради редкого расхождения — лишнее.
    На тех же тактах — время «последний раз на связи» живых сессий; каждый
    такт — такт набора обновлений (шаг шлюза, пропавшего посреди обновления,
    повторы догона)."""
    global _server
    if config.ROLE == "gateway":
        return
    if _server is None:
        _server = LinkServer(services)
    await _server.rekey_boot()                            # такт может опередить старт слушателя
    await _server.ensure()
    await _server.sweep_dead()
    await _server.send_roles()
    await _server.rekey_tick()
    await _server.i1_tick()
    if hasattr(services, "restore_wait_tick"):
        # после восстановления: кто не вышел на связь за срок — одно уведомление
        notes = await asyncio.to_thread(services.restore_wait_tick)
        if notes and _on_notes is not None:
            try:
                await _on_notes(list(notes))
            except Exception as e:                        # noqa: BLE001
                log.warning("канал линка: уведомления после восстановления не ушли: %s", e)
    n = _server.__dict__.get("_ticks", 0) + 1
    _server.__dict__["_ticks"] = n
    if n % 4 == 0:
        _server.deliver_soon()
        # «последний раз на связи» — правда и для живой сессии (~2 минуты)
        live = [sid for sid, s in _server._sessions.items() if s.hello]
        if live and hasattr(services, "gwlink_seen_touch"):
            try:
                await asyncio.to_thread(services.gwlink_seen_touch, live)
            except Exception as e:                        # noqa: BLE001
                log.warning("канал линка: время связи не записано: %s", e)


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


from .upd import set_on_fleet  # noqa: E402

__all__ = ["set_on_fleet", "set_on_notes", "_on_notes", "set_on_rekeyed", "_on_rekeyed", "LinkServer", "HELLO_TIMEOUT", "DEFAULT_PORT", "_Session", "channel_port", "vps_address", "gw_address",
           "set_on_installed", "ensure", "liveness_tick", "monitor_tick", "current", "shutdown"]
