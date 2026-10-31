"""
watch.py — часть пакета reserve агента (см. __init__): наблюдатель резерва —
счётчик `rx` линка `awgrsv`, пропуски keepalive и проба `ping` по гостевому
каналу по подозрению (util/livestep). Взгляд на сервер AWG — зонд кворума
(quorum.py), наблюдателя основы здесь нет. Строка панели (Т-РА1), здоровье
(Т-РА2) и поле `rsv` снимка.
"""
from __future__ import annotations

import time

from awgbot.core import settings
from awgbot.infra.gwguard import rsv as rsvlink
from awgbot.util import livestep, timeutil

from ..base import log


class ReserveWatchMixin:
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
        if hasattr(self, "reserve_counters_reset"):
            self.reserve_counters_reset()                     # счётчики линка — свежие на этот такт
        actions: list[tuple] = []
        # резерв — по awgrsv
        lr = self._rsv_lv_reserve
        act = livestep.step(lr, now, self._reserve_rx(), suspect_s=suspect_s, probe_s=probe_s)
        if act == "ping":
            actions.append(("guest", "ping", {"n": lr.nonce}))
        elif act == "down":
            self.__dict__["_rsv_reserve_since"] = timeutil.now_iso()
            try:
                from .store import STORE_LOCK, _write
                with STORE_LOCK:
                    cur = self.reserve_section()
                    cur["reserve_down_last"] = self.__dict__["_rsv_reserve_since"]   # для Т-РА3 (Н3) после возврата связи
                    _write(cur, self._RSV_STORE)
            except Exception as e:                            # noqa: BLE001
                log.info("reserve: момент потери резерва не записан: %s", e)
            log.warning("reserve: резерв не отвечает по awgrsv")
            actions.append(("push",))
        elif act == "up":
            self.__dict__["_rsv_reserve_since"] = ""
            log.info("reserve: резерв снова на связи")
            actions.append(("push",))
        # пересылка — по правилу раздела каждый такт (факт записи кэшируется): событие
        # `state{serving}` приходит раньше факта записи, а раздел основы с `away`
        # применяется без действий
        try:
            actions += self.reserve_forward_decide()
        except Exception as e:                                # noqa: BLE001
            log.info("reserve: решение о пересылке: %s", e)
        for fn in (self.reserve_link_retry_tick, self.reserve_notes_retry, self.reserve_tgvia_tick):
            try:
                actions += fn(now)
            except Exception as e:                            # noqa: BLE001
                log.info("reserve: %s: %s", fn.__name__, e)
        return actions

    # ── счётчики ────────────────────────────────────────────────────────────
    def _reserve_rx(self) -> int | None:
        c = self.reserve_counters_cached() if hasattr(self, "reserve_counters_cached") else rsvlink.counters()
        return int(c[0]) if c else None

    # ── слова ───────────────────────────────────────────────────────────────
    def reserve_guest_pong_in(self, nonce: str) -> None:
        livestep.pong(self._rsv_lv_reserve, time.monotonic(), str(nonce or ""))

    def reserve_guest_word(self) -> None:
        livestep.word(self._rsv_lv_reserve, time.monotonic())

    # ── связь с резервом ────────────────────────────────────────────────────
    def reserve_seen(self) -> tuple[str, str]:
        """Связь с резервом: (ok | suspect | down, с какого момента)."""
        if not rsvlink.present():
            return "absent", ""
        lv = self._rsv_lv_reserve
        return lv.state, str(self.__dict__.get("_rsv_reserve_since") or "") if lv.state == "down" else ""
