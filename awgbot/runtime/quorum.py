"""
quorum.py — сокеты кворума резерва: ответчик лидера и пробы голосующего;
общее для основного бота, бота основы на резерве и агента шлюза.

Ответчик — UDP-сокет на втором адресе интерфейса AWG (у основы — в
пространстве хоста; у бота основы на резерве процесс и так живёт в зоне).
Пробы — по сокету на интерфейс зонда, созданному в пространстве `awg-quo`
(`util/netns.listen_in`: сокет остаётся в пространстве, где создан, а
процесс — в своём); интервал проб с джиттером задаёт правило голосующего.
Домен отдаёт действия кортежами: `("probe", имя, байты, (адрес, порт))`,
`("send", вид, тело)` — канал пары, `("gw", слот, вид, тело)` — шлюзу по
гостевому каналу, `("guest", вид, тело)` — резерву по гостевому каналу
(агент), `("link", вид, тело)` — своему серверу по каналу конфигурации
(агент), `("notify", [...])`.

Домен обязан давать: `quorum_runtime_plan()` → {"responder": (адрес, порт) | None,
"probes": {имя: {node_addr, resp, port}}}, `quorum_probe_in(data, src)` → ответ,
`quorum_reply_in(имя, data)` → действия, `quorum_tick()` → действия.
"""
from __future__ import annotations

import asyncio
import logging
import socket

log = logging.getLogger(__name__)


class _Responder(asyncio.DatagramProtocol):
    def __init__(self, services):
        self.services = services
        self.transport = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        asyncio.get_running_loop().create_task(self._reply(data, addr))

    async def _reply(self, data: bytes, addr) -> None:
        try:
            out = await asyncio.to_thread(self.services.quorum_probe_in, bytes(data), str(addr[0]))
        except Exception as e:                                # noqa: BLE001
            log.info("кворум: проба с %s не разобрана: %s", addr[0], e)
            return
        if out and self.transport is not None:
            try:
                self.transport.sendto(out, addr)
            except OSError as e:
                log.info("кворум: ответ %s не ушёл: %s", addr[0], e)


class _Prober(asyncio.DatagramProtocol):
    def __init__(self, services, name: str, on_actions):
        self.services = services
        self.name = name
        self.on_actions = on_actions
        self.transport = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        asyncio.get_running_loop().create_task(self._in(data))

    async def _in(self, data: bytes) -> None:
        try:
            actions = await asyncio.to_thread(self.services.quorum_reply_in, self.name, bytes(data))
        except Exception as e:                                # noqa: BLE001
            log.info("кворум: ответ зонду %s не разобран: %s", self.name, e)
            return
        if actions:
            await self.on_actions(actions)


class QuorumRuntime:
    """Ответчик и зонды одного процесса; `tick` держит их по плану домена."""

    def __init__(self, services, act):
        self.services = services
        self.act = act                                         # корутина-исполнитель действий домена
        self._resp: tuple[tuple[str, int], asyncio.DatagramTransport] | None = None
        self._probes: dict[str, tuple[dict, asyncio.DatagramTransport]] = {}
        self._failed: dict[str, float] = {}

    async def tick(self) -> None:
        try:
            plan = await asyncio.to_thread(self.services.quorum_runtime_plan)
        except Exception as e:                                # noqa: BLE001
            log.warning("кворум: план сокетов не собрался: %s", e)
            return
        await self._ensure_responder(plan.get("responder"), str(plan.get("responder_if") or ""))
        await self._ensure_probes(plan.get("probes") or {})
        try:
            actions = await asyncio.to_thread(self.services.quorum_tick)
        except Exception as e:                                # noqa: BLE001
            log.warning("кворум: такт: %s", e)
            return
        await self.run(actions)

    async def run(self, actions) -> None:
        rest = []
        for a in actions or []:
            if a and a[0] == "probe":
                await self._send_probe(str(a[1]), bytes(a[2]), tuple(a[3]))
            else:
                rest.append(a)
        if rest:
            await self.act(rest)

    async def _send_probe(self, name: str, data: bytes, target: tuple) -> None:
        ent = self._probes.get(name)
        if ent is None:
            return
        try:
            ent[1].sendto(data, (str(target[0]), int(target[1])))
        except OSError as e:
            log.info("кворум: проба %s не ушла: %s", name, e)

    async def _ensure_responder(self, want, iface: str = "") -> None:
        want = (str(want[0]), int(want[1])) if want else None
        if self._resp is not None and self._resp[0] != want:
            self._resp[1].close()
            self._resp = None
            log.info("кворум: ответчик закрыт")
        if want is None or self._resp is not None:
            return
        loop = asyncio.get_running_loop()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if iface:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, 25, iface.encode() + b"\x00")   # SO_BINDTODEVICE
                except OSError:
                    pass
            sock.bind(want)
            sock.setblocking(False)
            transport, _proto = await loop.create_datagram_endpoint(lambda: _Responder(self.services), sock=sock)
        except OSError as e:
            now = loop.time()
            if now - self._failed.get("resp", 0.0) > 300:
                self._failed["resp"] = now
                log.warning("кворум: ответчик на %s:%s не поднялся: %s", want[0], want[1], e)
            return
        self._resp = (want, transport)
        log.info("кворум: ответчик слушает %s:%s", want[0], want[1])

    async def _ensure_probes(self, want: dict) -> None:
        for name in list(self._probes):
            if name not in want or self._probes[name][0].get("node_addr") != want[name].get("node_addr"):
                self._probes.pop(name)[1].close()
                log.info("кворум: сокет зонда %s закрыт", name)
        loop = asyncio.get_running_loop()
        for name, rec in want.items():
            if name in self._probes:
                continue
            node_addr = str(rec.get("node_addr") or "")
            if not node_addr:
                continue
            if loop.time() - self._failed.get(name, -1e9) < 30:
                continue
            try:
                sock = await asyncio.to_thread(self._make_sock, node_addr)
                transport, _proto = await loop.create_datagram_endpoint(
                    lambda n=name: _Prober(self.services, n, self.run), sock=sock)
            except Exception as e:                            # noqa: BLE001 — пространства ещё нет, адрес не поднят
                self._failed[name] = loop.time()
                log.info("кворум: сокет зонда %s не создался: %s", name, e)
                continue
            self._probes[name] = (dict(rec), transport)
            log.info("кворум: сокет зонда %s открыт (%s)", name, node_addr)

    @staticmethod
    def _make_sock(node_addr: str) -> socket.socket:
        from awgbot.infra import quoprobe
        from awgbot.util import netns

        def make() -> socket.socket:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind((node_addr, 0))
            s.setblocking(False)
            return s
        return netns.listen_in(quoprobe.ns_path(), make)

    async def stop(self) -> None:
        if self._resp is not None:
            self._resp[1].close()
            self._resp = None
        for name in list(self._probes):
            self._probes.pop(name)[1].close()


_rt: QuorumRuntime | None = None


def runtime() -> QuorumRuntime | None:
    return _rt


async def tick(services, act) -> None:
    """Такт из планировщика роли: сокеты по плану, такт домена, действия — `act`."""
    global _rt
    if _rt is None:
        _rt = QuorumRuntime(services, act)
    await _rt.tick()


async def shutdown() -> None:
    global _rt
    if _rt is not None:
        await _rt.stop()
        _rt = None
