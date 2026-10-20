"""
watch.py — часть пакета reserve агента (см. __init__): взгляд шлюза на основу
и на резерв — пропуски keepalive и проба по подозрению (util/livestep), два
наблюдателя на двух линках.

Основа: счётчик `rx` линка слота (keepalive стороны сервера, с 4.0.0);
подозрение → проба: `ping` по каналу конфигурации (ответ `pong`) и, если у
шлюза есть аплинк на адрес основы, запрос к резолверу сервера через аплинк
(`<подсеть>.1:53`, сокет с SO_BINDTODEVICE) — тот же путь, что у клиентов:
«не вижу» только если не ответили оба. Смена взгляда → `seen` резерву по
гостевому каналу.

Резерв: счётчик `rx` линка `awgrsv`; подозрение → `ping` по гостевому каналу.
Строка панели (Т-РА1), здоровье (Т-РА2) и поле `rsv` снимка.
"""
from __future__ import annotations

import socket
import struct
import time

from awgbot.core import config, settings
from awgbot.infra.gwguard import rsv as rsvlink
from awgbot.util import livestep, timeutil

from ..base import log

UPLINK_PROBE_TIMEOUT = 2.0


class ReserveWatchMixin:
    @property
    def _rsv_lv_primary(self) -> livestep.Liveness:
        return self.__dict__.setdefault("_rsv_lv_primary", livestep.Liveness())

    @property
    def _rsv_lv_reserve(self) -> livestep.Liveness:
        return self.__dict__.setdefault("_rsv_lv_reserve", livestep.Liveness())

    def _rsv_thresholds(self) -> tuple[int, int]:
        return (settings.get_int("app.gateway.reserve_suspect_seconds", 40),
                settings.get_int("app.gateway.reserve_probe_seconds", 15))

    # ── такт ────────────────────────────────────────────────────────────────
    def reserve_watch_tick(self, now: float | None = None) -> list[tuple]:
        """Такт 5 с: оба наблюдателя. Действия: ("gw_ping", нонс) — проба основы по
        каналу конфигурации; ("guest", "ping", {n}) — проба резерва; ("guest",
        "seen", {...}) — взгляд сменился; ("push",) — снимок серверу; ("notify",
        [...]); ("open"/"close") — пересылка."""
        if not self.reserve_present():
            return []
        now = time.monotonic() if now is None else now
        suspect_s, probe_s = self._rsv_thresholds()
        actions: list[tuple] = []
        # основа — по линку слота
        lp = self._rsv_lv_primary
        act = livestep.step(lp, now, self._primary_rx(), suspect_s=suspect_s, probe_s=probe_s)
        if act == "ping":
            actions.append(("gw_ping", lp.nonce))
            if self._uplink_probe():
                livestep.word(lp, now)                   # аплинк ответил — основа жива для клиентов
        elif act == "down":
            self.__dict__["_rsv_primary_since"] = timeutil.now_iso()
            log.warning("reserve: сервер AWG не отвечает по линку слота и аплинку")
            actions += [("guest", "seen", self.reserve_seen_body()), ("push",)]
            actions += self.reserve_forward_decide()
        elif act == "up":
            self.__dict__["_rsv_primary_since"] = ""
            log.info("reserve: сервер AWG снова виден")
            actions += [("guest", "seen", self.reserve_seen_body()), ("push",)]
            actions += self.reserve_forward_decide()
        # резерв — по awgrsv
        lr = self._rsv_lv_reserve
        act = livestep.step(lr, now, self._reserve_rx(), suspect_s=suspect_s, probe_s=probe_s)
        if act == "ping":
            actions.append(("guest", "ping", {"n": lr.nonce}))
        elif act == "down":
            self.__dict__["_rsv_reserve_since"] = timeutil.now_iso()
            log.warning("reserve: резерв не отвечает по awgrsv")
            actions.append(("push",))
        elif act == "up":
            self.__dict__["_rsv_reserve_since"] = ""
            log.info("reserve: резерв снова на связи")
            actions.append(("push",))
        return actions

    # ── счётчики ────────────────────────────────────────────────────────────
    def _primary_rx(self) -> int | None:
        try:
            _up, _age, rx, _tx = self.link_status()
        except Exception:                                     # noqa: BLE001
            return None
        return int(rx)

    def _reserve_rx(self) -> int | None:
        c = rsvlink.counters()
        return int(c[0]) if c else None

    # ── слова ───────────────────────────────────────────────────────────────
    def reserve_gw_pong_in(self, nonce: str) -> None:
        """`pong` основы по каналу конфигурации."""
        livestep.pong(self._rsv_lv_primary, time.monotonic(), str(nonce or ""))

    def reserve_gw_word(self) -> None:
        """Любое сообщение сервера по каналу — жизнь основы."""
        livestep.word(self._rsv_lv_primary, time.monotonic())

    def reserve_guest_pong_in(self, nonce: str) -> None:
        livestep.pong(self._rsv_lv_reserve, time.monotonic(), str(nonce or ""))

    def reserve_guest_word(self) -> None:
        livestep.word(self._rsv_lv_reserve, time.monotonic())

    # ── взгляды ─────────────────────────────────────────────────────────────
    def reserve_primary_seen(self) -> tuple[str, str]:
        """(ok | suspect | down, с какого момента)."""
        lv = self._rsv_lv_primary
        return lv.state, str(self.__dict__.get("_rsv_primary_since") or "") if lv.state == "down" else ""

    def reserve_seen(self) -> tuple[str, str]:
        """Связь с резервом: (ok | suspect | down, с какого момента)."""
        if not rsvlink.present():
            return "absent", ""
        lv = self._rsv_lv_reserve
        return lv.state, str(self.__dict__.get("_rsv_reserve_since") or "") if lv.state == "down" else ""

    def reserve_seen_body(self) -> dict:
        """Тело `seen` гостевого канала: «вижу» — пусто, «не вижу» — с какого момента."""
        state, since = self.reserve_primary_seen()
        return {"seen": since if state == "down" else ""}

    # ── проба по аплинку ────────────────────────────────────────────────────
    def _uplink_probe(self) -> bool:
        """Запрос к резолверу сервера через аплинк — путь клиентов. Засчитывается,
        только пока Endpoint аплинка — адрес основы (после перерезолва на клон он
        смотрит на резерв)."""
        from awgbot.infra import gwguard
        from . import uplink as up
        try:
            iface = config.GW_UPLINK_IF or gwguard.uplink_interface()
        except Exception:                                     # noqa: BLE001
            iface = ""
        if not iface:
            return False
        host, _port = up.conf_endpoint(iface)
        own = str(self.reserve_section().get("self") or "")
        if not host or not own or (host != own and up.resolve(host) != up.resolve(own)):
            return False
        try:
            subnet = str(gwguard.unit_env("CLIENT_SUBNET") or "").split("/")[0]
            parts = subnet.split(".")
            resolver = ".".join(parts[:3] + ["1"]) if len(parts) == 4 else ""
        except Exception:                                     # noqa: BLE001
            resolver = ""
        if not resolver:
            return False
        return _dns_probe(resolver, iface)


def _dns_probe(resolver: str, iface: str, timeout: float = UPLINK_PROBE_TIMEOUT) -> bool:
    """Один DNS-запрос (A «.») к резолверу через интерфейс; любой ответ — жизнь."""
    qid = int(time.time()) & 0xFFFF
    msg = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + b"\x00" + struct.pack(">HH", 2, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        try:
            s.setsockopt(socket.SOL_SOCKET, 25, iface.encode() + b"\x00")   # SO_BINDTODEVICE
        except OSError:
            pass
        s.settimeout(timeout)
        s.sendto(msg, (resolver, 53))
        data, _ = s.recvfrom(512)
        return len(data) >= 12 and data[:2] == msg[:2]
    except OSError:
        return False
    finally:
        s.close()
