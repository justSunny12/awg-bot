"""
tick.py — часть пакета quorum (см. __init__): такт кворума и план сокетов для
runtime/quorum (основной бот дома и бот основы на резерве).

Такт (5 с, задание живости пары): состав своей линии по фактам, ключи и
адреса зондов, пиры-зонды и второй адрес на интерфейсе AWG (не чаще раза в
минуту), свои интерфейсы зондов в `awg-quo`, `qprobe` второму серверу по
смене, состав боту основы на резерве по локальному каналу, такты лидера и
голосующего. У бота основы на резерве — только ответчик на клоне и счёт
(`qcount` боту резерва).
"""
from __future__ import annotations

import json
import time

from awgbot.core import config

from ...gateway_link.common import log

PEERS_SYNC_EVERY_S = 60


class ReserveQuorumTickMixin:

    # ── план сокетов ────────────────────────────────────────────────────────
    def quorum_runtime_plan(self) -> dict:
        if getattr(self, "guest", False) or config.GUEST:
            if not self.__dict__.get("_guest_quorum"):
                return {"responder": None, "probes": {}}       # состава нет (жду состав) — ответчика нет
            return {"responder": self.reserve_quorum_responder_addr(), "probes": {}}
        if not self.reserve_paired():
            return {"responder": None, "probes": {}}
        return {"responder": self.reserve_quorum_responder_addr(), "probes": self.reserve_quorum_probe_ifaces(),
                "responder_if": config.AWG_INTERFACE}         # сокет привязан к интерфейсу: подделка с eth0 не доходит

    def quorum_probe_in(self, data: bytes, src: str) -> bytes | None:
        return self.reserve_quorum_probe_in(data, src)

    def quorum_reply_in(self, name: str, data: bytes) -> list[tuple]:
        return self.reserve_quorum_reply_in(name, data)

    # ── такт ────────────────────────────────────────────────────────────────
    def quorum_tick(self) -> list[tuple]:
        now = time.monotonic()
        if getattr(self, "guest", False) or config.GUEST:
            actions = self.reserve_quorum_leader_tick(now)
            body = self.reserve_quorum_qcount_body()
            if body is not None and body != self.__dict__.get("_quo_qcount_sent"):
                self.__dict__["_quo_qcount_sent"] = body
                actions.append(("guestctl", "qcount", body))
            return actions
        if not self.reserve_paired():
            self.__dict__.pop("_rsv_qleader", None)
            self._qvoters.clear()
            return []
        actions: list[tuple] = []
        if hasattr(self, "reserve_quorum_act_leftover"):
            self.reserve_quorum_act_leftover()
        m = self.reserve_quorum_refresh()                      # предложенный состав — по нему ключи и зонды
        if m is not None:
            body = self.reserve_qmem_body()
            if body is not None and self.reserve_online():
                actions.append(("send", "qmem", body))
            try:
                actions += self.reserve_quorum_keys_sync(m, self.reserve_quorum_wanted())
            except Exception as e:                            # noqa: BLE001
                log.warning("кворум: ключи кворума: %s", e)
            body = self.reserve_qkeys_body()
            if body is not None and self.reserve_online():
                actions.append(("send", "qkeys", body))
            try:
                self.reserve_probes_refresh(m)
            except Exception as e:                            # noqa: BLE001
                log.warning("кворум: ключи зондов: %s", e)
            if now - float(self.__dict__.get("_quo_peers_sync_at") or 0.0) >= PEERS_SYNC_EVERY_S or \
                    self.__dict__.get("_quo_peers_sync_g") != int(m.g):
                self.__dict__["_quo_peers_sync_at"], self.__dict__["_quo_peers_sync_g"] = now, int(m.g)
                try:
                    self.reserve_probe_peers_sync(m)          # и без шлюзов: зонд второго сервера стоит при v = 1
                except Exception as e:                        # noqa: BLE001
                    log.warning("кворум: пиры-зонды: %s", e)
            body = self.reserve_qprobe_body()
            if body is not None and self.reserve_online():
                digest = body.pop("_digest")
                actions.append(("send", "qprobe", body))
                self.reserve_qprobe_sent(digest)
            if self.reserve_hosting():
                gq = self.reserve_quorum_for_guest()
                if gq is not None:
                    packed = json.dumps(gq, sort_keys=True)
                    if packed != self.__dict__.get("_quo_guest_sent"):
                        self.__dict__["_quo_guest_sent"] = packed
                        actions.append(("guest", "quorum", gq))
            else:
                self.__dict__.pop("_quo_guest_sent", None)
        try:
            self.reserve_probes_apply()
        except Exception as e:                                # noqa: BLE001
            log.warning("кворум: интерфейсы зондов: %s", e)
        actions += self.reserve_quorum_leader_tick(now)
        actions += self.reserve_quorum_voter_tick(now)
        actions += self.reserve_quorum_candidate_tick(now)
        actions += self.reserve_quorum_back_tick(now)
        if hasattr(self, "reserve_code_link_watch"):
            actions += self.reserve_code_link_watch()          # линия второй стороны без кворума — окно кода по линку пары
        return actions
