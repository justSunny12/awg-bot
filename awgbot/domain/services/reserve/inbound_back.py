"""
inbound_back.py — часть пакета reserve (см. __init__): «пользователи основы у
меня» — их возврат основе и пересылка хвоста (вынесено из inbound).

Ожидание реплики гостя после `stop` (`_tick_releasing`: молчит — `rep.bin` с
диска или `released` без реплики). `back` основы: пересылка хвоста (`--relay`, файервол с DNAT, `relay_flush`;
адрес основы — IP, имя резолвится сейчас; срыв открытия до смены записи — без
хвоста, сразу idle), учёт, `relay{until}`, `--wipe`, прежний пакет и доля —
прочь, `wiped`, Т-РУ5 → relay до `relay_max_days` или тишины
`relay_idle_hours` (по счётчику правила DNAT — nftguard.relay_packets) → idle.
Новое открытие при relay — пересылка уступает (`_relay_yield`).
"""
from __future__ import annotations

import time

from awgbot.core import config, settings
from awgbot.util import livestep, timeutil

from ..types import Notification
from ..gateway_link.common import log
from .state import _LOCK


class ReserveInboundBackMixin:

    def _relay_yield(self) -> None:
        """Фаза relay (пересылка хвоста) при новом открытии: правило DNAT снимается —
        иначе оно увело бы пользователей нового клона на основу; состояние — idle."""
        from awgbot.infra import rsvzone
        if self.reserve_in() != "relay":
            return
        try:
            rsvzone.teardown()
        except rsvzone.ZoneError as e:
            log.info("резерв: пересылка хвоста при новом открытии снята с ошибкой: %s", e)
        self.reserve_set_in("idle", step="", relay_to="", relay_until="")
        log.info("резерв: пересылка хвоста снята — зона нужна новому переезду")

    @staticmethod
    def _relay_host_ip(host: str) -> str:
        """Адрес основы для DNAT хвоста: IPv4-литерал как есть; имя — резолвится
        сейчас (первый A); не резолвится — пусто (хвоста не будет, зона снимается)."""
        import ipaddress
        import socket
        if not host:
            return ""
        try:
            return str(ipaddress.IPv4Address(host))
        except ValueError:
            pass
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as e:
            log.warning("резерв: адрес основы %s для пересылки хвоста не резолвится: %s", host, e)
            return ""
        return str(infos[0][4][0]) if infos else ""

    def _tick_releasing(self, inn: dict) -> list[tuple]:
        from awgbot.infra import rsvzone
        if inn.get("released_at"):
            return []
        if livestep.mono_expired(inn.get("deadline"), time.monotonic()):
            # гость не ответил — отдать последний снимок с диска
            try:
                blob = rsvzone.REP_PATH.read_bytes()
            except OSError:
                blob = b""
            if blob:
                return self.reserve_guest_replica_in(blob)
            log.warning("резерв: реплики нет ни от гостя, ни на диске — released без реплики")
            self.rsv_update("rsv_in", released_at=timeutil.now_iso())
            return [("send_released", {"gen": int(inn.get("gen") or 0), "blob": ""})]
        return []

    def reserve_back_in(self) -> list[tuple]:
        """`back` основы: пересылка хвоста, учёт, `relay{until}`, снятие, `wiped`, Т-РУ5."""
        from awgbot.infra import rsvzone
        from awgbot.domain.notices import reserve as rn
        inn = self._inn()
        if str(inn.get("state") or "") == "relay":
            return [("send", "relay", {"until": str(inn.get("relay_until") or "")}), ("send", "wiped", {})]   # повтор
        if str(inn.get("state") or "") not in ("releasing", "serving", "taking"):
            # зоны нет — основе нечего ждать; журнал данных гостя и rep.bin под прежним K — прочь
            try:
                self.guest_jrn_store().wipe()
                rsvzone.REP_PATH.unlink(missing_ok=True)
            except OSError as e:
                log.info("резерв: остатки гостя на диске не сняты: %s", e)
            return [("send", "wiped", {})]
        name = self.reserve_peer_name()
        # пересылка хвоста нужна, только если пользователи могли прийти на клон: зона обслуживала
        # или процесс открытия дошёл до смены записи; срыв раньше — сразу idle
        step = str(rsvzone.open_status().get("step") or "")
        had_clients = str(inn.get("state") or "") != "taking" or step in ("record_set", "record_fact", "done")
        if str(inn.get("state") or "") == "taking":
            try:
                self._rsvopen_stop()                          # переезд сорвался на открытии — юнит прочь
            except Exception as e:                            # noqa: BLE001
                log.info("резерв: юнит процесса открытия не остановлен: %s", e)
        peer = self.reserve_peer_state()
        host = str(peer.get("self_host") or (self.reserve_peer() or {}).get("host") or "")
        host = self._relay_host_ip(host)                      # в правило nft — адрес: имя резолвится при загрузке таблицы
        ports = [int(p) for p in (peer.get("ports") or []) if isinstance(p, int)]
        to = f"{host}:{ports[0]}" if host and ports and had_clients else ""
        until = timeutil.to_iso(timeutil.shift(timeutil.now(), settings.get_int("app.reserve.relay_max_days", 7) * 86400))
        try:
            self.reserve_traffic_in_close()
        except Exception as e:                                # noqa: BLE001
            log.info("резерв: учёт при возврате: %s", e)
        try:
            if to:
                rsvzone.relay(to)
            else:
                rsvzone.teardown()
        except rsvzone.ZoneError as e:
            log.warning("резерв: зона при возврате: %s", e)
        actions: list[tuple] = [("fw",)] + ([("relay_flush",)] if to else []) + [("send", "relay", {"until": until})]
        try:
            rsvzone.wipe()
        except rsvzone.ZoneError as e:
            log.warning("резерв: хранилище гостя не снято: %s", e)
        self.reserve_pkg_in_drop()
        with _LOCK:
            self.reserve_set_in("relay", kind=str(inn.get("kind") or ""), relay_to=to, relay_until=until,
                                relay_at=timeutil.now_iso(), step="")
        actions.append(("send", "wiped", {}))
        tr = self._reserve_traffic_view().get("in") or ""
        actions.append(("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.hosting_ended(name, tr))]))
        log.info("резерв: пользователи и бот «%s» вернулись к нему; пересылка хвоста до %s", name, until)
        return actions

    def _tick_relay(self, inn: dict) -> list[tuple]:
        from awgbot.infra import rsvzone
        until = str(inn.get("relay_until") or "")
        done = False
        try:
            done = bool(until) and timeutil.parse_iso(until) <= timeutil.now()
        except ValueError:
            done = True
        idle_h = settings.get_int("app.reserve.relay_idle_hours", 2)
        if not done and idle_h:
            last = self._relay_last_traffic(inn)
            try:
                done = bool(last) and timeutil.elapsed(timeutil.parse_iso(last)) >= idle_h * 3600
            except ValueError:
                done = True
        if not done:
            return []
        try:
            rsvzone.teardown()
        except rsvzone.ZoneError as e:
            log.info("резерв: пересылка хвоста снята с ошибкой: %s", e)
        self.reserve_set_in("idle", step="", relay_to="", relay_until="")
        return [("fw",), ("send", "st", self.reserve_st_body())]

    def _relay_last_traffic(self, inn: dict) -> str:
        """Момент последнего пакета через правило пересылки (по счётчику правила DNAT
        в таблице хоста): счётчик вырос с прошлого такта — «сейчас»; счётчика нет —
        начало пересылки. Тишина считается от него."""
        from awgbot.infra import nftguard
        last = str(inn.get("relay_last_traffic") or inn.get("relay_at") or "")
        try:
            pkts = nftguard.relay_packets()
        except Exception as e:                                # noqa: BLE001
            log.info("резерв: счётчик пересылки хвоста не прочитан: %s", e)
            pkts = None
        if pkts is None:
            return last
        seen = inn.get("relay_pkts")
        if seen is not None and int(pkts) > int(seen):       # меньше — перерисовка таблицы обнулила счётчик
            last = timeutil.now_iso()
        if seen is None or int(pkts) != int(seen) or not inn.get("relay_last_traffic"):
            self.rsv_update("rsv_in", relay_pkts=int(pkts), relay_last_traffic=last)
        return last
