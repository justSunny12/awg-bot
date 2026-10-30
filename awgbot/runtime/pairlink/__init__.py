"""
pairlink/ — канал пары серверов AWG внутри линка пары (части: common — сессия,
ключ, адреса; server — сторона пригласившего (слушатель); client — сторона
принявшего (подключение с отступом); inbound — разбор обычной сессии).

Слушает пригласивший на своём адресе в /30 линка пары, подключается принявший
— так же, как канал шлюзов: слушатель только на адресе линка, публичный адрес
порт канала не показывает. Конверт — `util/gwlink` (подпись, нонсы, добивка)
со своим доменом ключа (`util/pairproto`): до сопряжения ключ — из закрытого
ключа стороны принявшего (он в приглашении), после — из PSK линка.

В простое прикладных сообщений нет: `st` — по смене и в начале сессии,
`ping` — только по подозрению (живость — keepalive линка, `services/reserve/liveness`).
TCP-keepalive ядра на сокете — как у канала шлюзов.

Крючки main: `set_on_event` — события сопряжения в чат (правка живого
сообщения), `set_on_notes` — уведомления домена.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from .common import DEFAULT_PORT, channel_port, pair_hosts

log = logging.getLogger(__name__)

_on_event = None
_on_notes = None
_link = None          # PairServer | PairClient | None


def set_on_event(fn) -> None:
    global _on_event
    _on_event = fn


def set_on_notes(fn) -> None:
    global _on_notes
    _on_notes = fn


def online() -> bool:
    return bool(_link is not None and _link.online)


async def ensure(services) -> None:
    """Поднять нужную сторону по роли в паре (или в сопряжении): слушатель у
    пригласившего, клиент у принявшего; роли нет — снять."""
    global _link
    role = await asyncio.to_thread(services.reserve_link_role)
    want = {"inviter": "server", "acceptor": "client", "": ""}[role]
    have = "" if _link is None else ("server" if hasattr(_link, "ensure") else "client")
    if have and have != want:
        with contextlib.suppress(Exception):
            await _link.stop()
        _link = None
    if want == "server":
        if _link is None:
            from .server import PairServer
            _link = PairServer(services)
        await _link.ensure()
    elif want == "client":
        if _link is None:
            from .client import PairClient
            _link = PairClient(services)
        _link.start()


async def shutdown() -> None:
    global _link
    if _link is not None:
        with contextlib.suppress(Exception):
            await _link.stop()
        _link = None


async def send(kind: str, body: dict | None = None) -> bool:
    return bool(_link is not None and await _link.send(kind, body))


async def send_package(built: dict) -> bool:
    """Пакет частями: pkg{gen, fp, i, n, blob}; False — сессии нет или обрыв."""
    from awgbot.util import pairproto
    parts = pairproto.chunks(str(built["blob"]))
    for i, blob in enumerate(parts):
        body = {"gen": int(built["gen"]), "fp": str(built["fp"]), "i": i, "n": len(parts), "blob": blob}
        if built.get("jrn"):
            body["jrn"] = dict(built["jrn"])                  # снимок журнала: что в нём учтено (открыто)
        ok = await send("pkg", body)
        if not ok:
            return False
    return True


async def send_released(built: dict) -> bool:
    """Реплика при передаче бота частями: released{gen, i, n, blob}; пустой blob — одна часть."""
    from awgbot.util import pairproto
    parts = pairproto.chunks(str(built.get("blob") or "")) or [""]
    jrn = {k: int(built[k]) for k in ("epoch", "last") if built.get(k) is not None}   # журнал: без реплики
    for i, blob in enumerate(parts):
        if not await send("released", {"gen": int(built.get("gen") or 0), "i": i, "n": len(parts), "blob": blob, **jrn}):
            return False
    return True


async def pair_accepted() -> None:
    if _link is not None and hasattr(_link, "pair_accepted"):
        await _link.pair_accepted()


async def pair_wrong(left: int) -> None:
    if _link is not None and hasattr(_link, "pair_wrong"):
        await _link.pair_wrong(left)


async def pair_close(why: str, notify: bool = False) -> None:
    if _link is not None and hasattr(_link, "pair_close"):
        await _link.pair_close(why, notify=notify)


def wake() -> None:
    if _link is not None and hasattr(_link, "wake"):
        _link.wake()


__all__ = ["DEFAULT_PORT", "channel_port", "pair_hosts", "set_on_event", "set_on_notes", "online", "ensure",
           "shutdown", "send", "send_package", "pair_accepted", "pair_wrong", "pair_close", "wake"]
