"""
linkclient/ — сторона шлюза для канала ВПС ↔ шлюз (части: common — задержки и пределы;
session — подключение и hello; outbox — отправка; feeds — свои списки, сервисы, фиды,
документ конфигурации и cfg_ack (новый токен бота — перезапуск агента после ответа);
dispatch — разбор входящих; rekey — смена ключей линка: приглашение, переключение,
частый такт перехода; uplink — такт аплинка: откат прерванной транзакции и
аварийное правило; keep — копия настроек шлюза для бэкапа сервера: ответы на
hello и свой блок `keep`; upd — команда набора обновлений: `upd`, `upd_commit`,
`upd_revert` и ответ `upd_ack`).

Коннект устанавливает ТОЛЬКО шлюз: на нём не появляется ни одного слушающего
сокета, и механика работает за любым NAT. Ответные пакеты сервера проходят в
`tunnel_in` по `ct state established` — на шлюзе не нужно ни одного нового
правила файервола.

Периодической задачи здесь нет. Сессия держится открытой, и в простое по ней не
уходит ни одного прикладного сообщения: снимок отправляется при подключении,
дальше — только когда факт или вердикт действительно сменились. TCP-keepalive
ядра на сокете включён (`gwlink.tcp_keepalive`) — внутри линка, снаружи не
виден, полуоткрытую сессию после жёсткого ребута ВПС добивает за ~23 минуты;
отправка, не сданная за `SEND_TIMEOUT`, рвёт сессию сразу. Переподключение — с
бэкоффом и джиттером: ровный ритм попыток был бы тем же маячком, только на
уровне TCP. Бэкофф сбрасывает только сессия, в которой сервер ответил. Линк
лёг и поднялся (`on_tick`) — сессия рвётся и ставится заново, а спящий бэкофф
будится с нулевого шага. Канал свежий для фидов, пока жива сессия
(`services.channel.online`; слово сервера не мерило — в здоровой сессии он
молчит); запас на своё скачивание фидов продлевает только сессия, в которой
сервер ответил. После операций из чата
(«🚑 Восстановить», документ конфигурации) снимок уходит сразу (`poke`). Байты канала
копятся в `services.channel`: зонд выхода наружу вычитает их из счётчиков линка.

Канал обязателен: агент держит его, пока на шлюзе есть конфиг линка (без него
каналу не по чему ехать — ключ подписи выводится из ключа линка). Рубильника в
юните нет.

С ВПС по той же сессии приходят только данные из закрытого списка: `cfg` —
документ конфигурации целиком (ответ `cfg_ack`, `domain/gateway/cfg`; с
`token1` в нём может быть раздел `token` — новый токен своего бота, после
ответа агент перезапускается на нём), `lists` — фиды локальной сети
(`apply_lan_feeds`, ответ `lists_ack`; `lists_ok` — у шлюза уже те же, запас
своего скачивания отсчитывается от конца сессии), `role` — несёт ли слот
трафик (`active`), есть ли другой слот с устройством (`standby`), имя слота
(`name`), возможности сервера (`caps`), его версия (`server` — условие
`needs_main` команды набора обновлений) и адреса в
туннеле своей сети (`lan_virtual` — только для показа; старый сервер шлёт
только `active`), `ask snap` — просьба о полном снимке, `peer_svc` — записи SMB
соседних сетей (`apply_peer_services`, ответ `peer_svc_ack`), `own_set` —
канон своих списков, общих для всех шлюзов (`apply_own_lists`, ответ
`own_ack`), смена ключей линка — `link_next` (приглашение, ответ
`link_ack`), `link_go` (переключение), `link_cancel` (сервер вернулся на
прежние или снял переход — «согласие» снимается); итог перехода шлюз сообщает
`link_done` (часть `rekey`); команда набора обновлений — `upd` (проверки и
ступень — у агента, ответ `upd_ack`), `upd_commit` (набор зафиксирован —
удержанное прежнее прочь), `upd_revert` (откат набора). Сопряжение (шлюз, подключённый командой, `join`) — своей сессией: hello с
`join`, `eph`, `host`; с ВПС — `join_eph`, `join_wrong`, `join_refused`,
`join_ok` и смена ключей `auto=join` (часть `join`). В обратную сторону, кроме снимка и ответов, —
свои SMB-серверы (`svc`), правки своих списков (`own_ev`: после кнопки в чате
сразу, `own_changed`; правки мимо агента находит сверка файлов на тике
монитора, `own_tick`), «установлен» в первый час после установки
(`installed`) и копию своих настроек (`keep`: с keep1 сервер отвечает на hello
`keep_ok`, `keep_none` или `keep_back` — блок для новой установки или после
восстановления сервера; своё агент шлёт только после этого ответа).
Неизвестный вид пропускается с записью в журнал: новый ВПС со старым агентом
не рвёт сессию.
Повтор отсекают нонсы сессии (gwlink, proto 2): hello несёт наш нонс, первое
сообщение сервера — его; дальше подписываем нонсом сервера, проверяем своим.
Часы хоста в проверку не входят — шлюз без RTC поднимает канал и до NTP.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import random          # noqa: F401 — тесты подменяют random.uniform через пакет
import subprocess
import time

from awgbot.core import config
from awgbot.util import gwlink

from .common import DEFAULT_PORT, SEND_TIMEOUT, _BACKOFF
from .session import SessionMixin
from .outbox import OutboxMixin
from .feeds import FeedsMixin
from .dispatch import DispatchMixin
from .rekey import RekeyMixin
from .uplink import UplinkMixin
from .keep import KeepMixin
from .relay import RelayMixin
from .upd import UpdMixin
from .join import JoinMixin

log = logging.getLogger(__name__)


def enabled() -> bool:
    """Каналу есть по чему ехать: конфиг линка на месте (из него — ключ подписи)."""
    import os
    return os.path.isfile(config.GW_LINK_CONF)


def server_port() -> int:
    """Порт канала: идёт сопряжение — из файла сопряжения (токен этого
    сервера: у шлюза, подключённого раньше, юнит обвязки ещё прежний, flow-9);
    иначе — из юнита обвязки."""
    from awgbot.infra import gwguard
    try:
        import json
        from awgbot.domain.gateway.join import JOIN_FILE
        with open(JOIN_FILE, encoding="utf-8") as f:
            st = json.loads(f.read() or "{}")
        # только живое сопряжение (или доведённое, до join_done_ok): файл
        # закрытого (refused) — не про порт этого сервера (join-4)
        port = int(st.get("channel_port") or 0) if st.get("state") in ("pairing", "accepted", "linked",
                                                                         "done") else 0
        if 0 < port < 65536:
            return port
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    raw = gwguard.unit_env("LINK_CHANNEL_PORT")
    if raw.isdigit():
        return int(raw)
    return DEFAULT_PORT


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



class LinkClient(SessionMixin, OutboxMixin, FeedsMixin, DispatchMixin, RekeyMixin, UplinkMixin, KeepMixin,
                 RelayMixin, UpdMixin, JoinMixin):
    """Клиент канала на шлюзе — собирается из частей пакета (см. шапку)."""


_client: LinkClient | None = None
_conf_gone_since: float | None = None   # с какого момента (monotonic) нет конфига линка при живом клиенте
CONF_GONE_STOP = 600                    # столько без конфига линка — снятие, а не перезапись: клиента гасим
_notify = None
_ask = None
_edit = None
def set_edit(fn) -> None:
    """Крючок правки (async fn(chat_id, message_id, text) -> bool): итог смены
    ключей и отзыв правят само сообщение приглашения. Ставит main."""
    global _edit
    _edit = fn


def set_ask(fn) -> None:
    """Крючок вопроса человеку (async fn(section)): документ с сервера принёс
    почту или аплинк, отличные от стоящих, ключи линка или настройки шлюза
    с сервера — показать вопрос живым меню."""
    global _ask
    _ask = fn


def set_notify(fn) -> None:
    """Корутина `fn(text, *, silent=False)` — отправить человеку в чат агента
    (silent — без звука). Ставит main: у клиента канала своего бота нет."""
    global _notify
    _notify = fn


def ensure(services) -> LinkClient | None:
    """Поднять клиента, когда есть конфиг линка. Роль client сюда не ходит.
    Юнит обвязки канал не выключает: запущенный клиент живёт до конца процесса,
    а без конфига линка новый не заводится — подписать hello ему нечем."""
    global _client
    if config.ROLE != "gateway":
        return None
    if not enabled():
        return None
    if _client is None:
        _client = LinkClient(services)
        _client.rekey_kick()                              # перезапуск посреди перехода — частый такт сразу
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


def view() -> dict:
    """Состояние канала для текстов панели одним словарём: открыта ли сессия,
    роль по последнему слову сервера."""
    on = online()
    return {"online": on, "role": role() if on else ""}


async def on_tick(services) -> None:
    """После тика монитора: отправить дельту, если что-то действительно
    изменилось. Зовётся из задачи планировщика, а не своим расписанием —
    своего расписания у канала нет по замыслу.

    Здесь же — переподключение по восстановлению линка. Жёсткий ребут ВПС не
    шлёт FIN, а агент в простое сам ничего не отправляет и RST не получит:
    сессия висела бы «открытой» днями, подавляя своё скачивание фидов. Линк
    лёг и поднялся — значит, прежней сессии на той стороне нет наверняка;
    рвём и подключаемся заново. Ноль пакетов сверх того, что и так идёт."""
    if await _retire_without_conf():
        return
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
        if was is False and link_ok:
            if client._writer is not None:
                log.info("канал линка: линк восстановился — переподключаюсь")
                with contextlib.suppress(Exception):
                    client._writer.close()
                return
            client._wake.set()                            # бэкофф спит — будим, шаг с нуля
    await client.push()
    for step in (client.retry_peer_services, client.own_tick, client.rekey_tick_monitor, client.uplink_tick,
                 client.keep_tick):
        try:
            await step()
        except Exception as e:                            # noqa: BLE001
            log.warning("канал линка: %s на тике не прошёл: %s", step.__name__, e)


async def _retire_without_conf() -> bool:
    """Конфиг линка пропал при живом клиенте. Мгновение — это перезапись
    (файл конфигурации, документ с сервера): сессию не трогаем. Дольше
    CONF_GONE_STOP — обвязку сняли (--rollback, снятие шлюза): клиент гасится
    и не стучится в несуществующий линк до конца процесса. True — погашен."""
    global _client, _conf_gone_since
    if config.ROLE != "gateway" or enabled() or _client is None:
        _conf_gone_since = None
        return False
    now = time.monotonic()
    if _conf_gone_since is None:
        _conf_gone_since = now
        return False
    if now - _conf_gone_since < CONF_GONE_STOP:
        return False
    log.info("канал линка: конфига линка нет %d мин — обвязку сняли, клиент канала гашу",
             int((now - _conf_gone_since) // 60))
    client, _client, _conf_gone_since = _client, None, None
    if client._writer is not None:
        with contextlib.suppress(Exception):
            client._writer.close()
    await client.stop()
    return True


async def poke(services) -> None:
    """После операции из чата агента («🚑 Восстановить»): поднять клиента, если
    его ещё нет, и сразу отправить дельту — человек ждёт результата на сервере
    AWG сейчас, а не через тик. Заодно — внеочередной обзор сервисов: прогон
    обвязки мог включить или выключить доступ между подсетями."""
    client = ensure(services)
    if client is not None:
        with contextlib.suppress(Exception):
            await client.push()
    try:
        await services_changed(services)
    except Exception as e:                                # noqa: BLE001
        log.warning("канал линка: внеочередной обзор SMB не прошёл: %s", e)


def _live_client() -> "LinkClient | None":
    """Клиент с открытой сессией — или None."""
    client = _client
    return client if client is not None and client._writer is not None else None


async def own_changed(services) -> bool:
    """Кнопка своих списков в чате агента: сверка сразу и правки серверу, если
    канал жив; нет — уйдут при подключении. Всё, что человек сделал руками,
    уходит по каналу немедленно. True — сверка нашла
    новые правки (неотправленные прежние тоже уходят, но хвост в чате — только
    за новые)."""
    reconcile = services.own_reconcile
    client = _client
    if client is not None:
        async with client._own_lock:
            changed = await asyncio.to_thread(reconcile)
    else:
        changed = await asyncio.to_thread(reconcile)
    if _live_client() is not None and (changed or await asyncio.to_thread(services.own_unsent)):
        await client.push_own()
    return changed


async def services_changed(services) -> None:
    """Задача обзора: изменился свой список
    — отправить серверу, если канал жив; нет — уйдёт целиком при подключении."""
    changed = await asyncio.to_thread(services.services_scan)
    client = _live_client()
    if changed and client is not None:
        await client.push_services()


async def shutdown() -> None:
    global _client
    if _client is not None:
        _client.rekey_stop()                              # частый такт перехода — вместе с клиентом
        await _client.stop()
        _client = None


__all__ = ["LinkClient", "DEFAULT_PORT", "SEND_TIMEOUT", "_BACKOFF", "enabled", "server_port", "server_address",
           "set_notify", "set_ask", "set_edit", "_edit", "report_cfg_answer", "report_rekey_answer", "report_keep_answer", "relay_backup", "relay_reserve", "relay_release", "_ask", "ensure", "role", "online", "view", "on_tick", "poke", "own_changed",
           "services_changed", "shutdown", "asyncio", "random", "gwlink"]


async def report_cfg_answer(services, answer: dict) -> None:
    """Человек ответил на вопрос документа: ответ серверу — сразу, если сессия
    открыта, иначе из очереди первым сообщением новой сессии."""
    ack = await asyncio.to_thread(services.cfg_answer_ack, answer)
    if not ack:
        return
    await asyncio.to_thread(services.cfg_ack_pending_put, ack)
    if _client is not None and _client._writer is not None:
        await _client.flush_cfg_ack()


async def report_rekey_answer(services, accept: bool) -> dict | None:
    """Человек ответил на приглашение о ключах линка: состояние перехода и ответ
    серверу (если сессия открыта; иначе сервер повторит link_next с тем же id
    при переподключении, и шлюз ответит своим состоянием)."""
    reply = await asyncio.to_thread(services.rekey_answer, accept)
    if reply and _client is not None:
        await _client.send_rekey_answer(reply)
    return reply



_relay_busy = False                     # идёт передача бэкапа серверу: одна за раз


def relay_reserve() -> bool:
    """Занять передачу бэкапа (синхронно, до первого await вызывающего): занято —
    False, второй файл ждёт итога первого."""
    global _relay_busy
    if _relay_busy:
        return False
    _relay_busy = True
    return True


def relay_release() -> None:
    global _relay_busy
    _relay_busy = False


async def relay_backup(blob: bytes, name: str) -> dict:
    """Бэкап сервера из чата бота шлюза — серверу по каналу (linkclient/relay).
    Клиента нет — сервер не на связи."""
    if _client is None:
        return {"ok": False, "why": "offline", "error": ""}
    return await _client.send_backup(blob, name)


async def report_keep_answer(services, accept: bool) -> dict:
    """Человек ответил на вопрос о настройках из бэкапа: применить (или нет) и
    отправить серверу свой блок — сразу, если сессия открыта; иначе при
    следующей (сервер, получив прежний отпечаток, спросит; на тот же блок агент
    ответит своим без вопроса)."""
    res = await asyncio.to_thread(services.keep_answer, accept)
    if res.get("ok") and not res.get("error") and _client is not None and _client._writer is not None:
        _client._keep_ready = True
        await _client.keep_push(force=True)
    return res
