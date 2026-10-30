"""
gwguest.py — часть пакета reserve (см. __init__): гостевой канал шлюзов основы
со стороны резерва — сессии, живость (счётчики сторон зоны, `ping`/`pong`),
слово `state{serving|idle, move, e}` шлюзам и первые слова новой сессии
(`state`, `qstate` с эпохой линии). Взгляды и решения о переезде — только у
кворума (quorum/*): `qview` и `qshare` шлюза уходят туда.

Имён шлюзов основы у резерва нет: в текстах резерва шлюз — «шлюз N».
"""
from __future__ import annotations

import json
import time

from awgbot.core import settings
from awgbot.util import livestep, timeutil

from ..gateway_link.common import log


class ReserveGwGuestMixin:
    # ── память процесса ─────────────────────────────────────────────────────
    @property
    def _gwv(self) -> dict:
        """слот → {"online", "lv": Liveness, "down_since", "since", "caps"}."""
        return self.__dict__.setdefault("_rsv_gwviews", {})

    def _gw_thresholds(self) -> tuple[int, int]:
        return (settings.get_int("app.reserve.suspect_seconds", 40), settings.get_int("app.reserve.probe_seconds", 15))

    @staticmethod
    def reserve_gw_name(slot: int) -> str:
        """Шлюз основы в текстах резерва — по номеру слота: имён второй стороне нет."""
        return f"шлюз {int(slot)}"

    # ── сессии гостевого канала ─────────────────────────────────────────────
    def reserve_guest_opened(self, slot: int, hello: dict) -> list[tuple]:
        """`hello{slot, caps}` шлюза: в память; ему — `state` и эпоха линии (`qstate`) сразу."""
        rec = self._gwv.setdefault(int(slot), {"lv": livestep.Liveness()})
        rec.update(online=True, caps=list(hello.get("caps") or []), since=timeutil.now_iso(), down_since="")
        livestep.word(rec["lv"], time.monotonic())
        log.info("резерв: шлюз %s основы на гостевом канале", slot)
        actions = [("gw", int(slot), "state", self._state_body())]
        qs = self._quorum_qstate_body()
        if qs is not None:
            actions.append(("gw", int(slot), "qstate", qs))   # эпоха линии первым словом новой сессии (§3.3)
        return actions

    def _quorum_line_epoch_for_gw(self) -> int:
        """Эпоха линии основы, как её знает кандидат: из `st.jrn` второй стороны и ответов зонда."""
        if not hasattr(self, "reserve_quorum_side") or not self.reserve_quorum_side():
            return 0
        from awgbot.util import probenet
        other = probenet.other_side(self.reserve_quorum_side())
        v = self._qvoters.get(f"awgq{other}h") if hasattr(self, "_qvoters") else None
        known = int(self._quorum_known_epoch(other)) if hasattr(self, "_quorum_known_epoch") else 0
        return max(known, int(v.epoch) if v is not None else 0)

    def _quorum_qstate_body(self) -> dict | None:
        if not hasattr(self, "reserve_quorum_peer_line") or not self.reserve_quorum_peer_line().get("active"):
            return None
        e = self._quorum_line_epoch_for_gw()
        if not e:
            return None
        from awgbot.util import probenet
        l = probenet.other_side(self.reserve_quorum_side())
        holder = "self" if self.reserve_hosting() else l
        return {"l": l, "e": e, "holder": holder, "r": "cand"}

    def reserve_guest_closed(self, slot: int) -> list[tuple]:
        rec = self._gwv.get(int(slot))
        if rec:
            rec["online"] = False
            rec["closed_mono"] = time.monotonic()            # обрыв сессии — подозрение, не «недоступен»
        return []

    def reserve_guest_in(self, slot: int, kind: str, body: dict) -> list[tuple]:
        """Слово шлюза по гостевому каналу."""
        rec = self._gwv.setdefault(int(slot), {"lv": livestep.Liveness(), "online": True})
        livestep.word(rec["lv"], time.monotonic())
        if kind == "ping":
            return []                                         # pong отдаёт слушатель канала сам
        if kind == "pong":
            livestep.pong(rec["lv"], time.monotonic(), str(body.get("n") or ""))
            return []
        if kind in ("qview", "qshare") and hasattr(self, "reserve_quorum_guest_in_kind"):
            return self.reserve_quorum_guest_in_kind(int(slot), kind, body)
        return []

    # ── такт ────────────────────────────────────────────────────────────────
    def reserve_gwguest_tick(self) -> list[tuple]:
        """Такт 5 с: живость шлюзов по счётчикам сторон зоны, проба `ping`, слово `state` по смене."""
        if not self.reserve_paired():
            self.__dict__.pop("_rsv_gwviews", None)
            return []
        suspect_s, probe_s = self._gw_thresholds()
        now = time.monotonic()
        actions: list[tuple] = []
        for slot, rec in list(self._gwv.items()):
            if not rec.get("online"):
                continue
            act = livestep.step(rec["lv"], now, self.reserve_zone_link_rx(slot), suspect_s=suspect_s, probe_s=probe_s)
            if act == "ping":
                actions.append(("gw", slot, "ping", {"n": rec["lv"].nonce}))
            elif act == "down":
                rec["down_since"] = timeutil.now_iso()
                log.warning("резерв: шлюз %s основы не отвечает по линку", slot)
            elif act == "up":
                rec["down_since"] = ""
        actions += self.reserve_gw_state_actions()
        return actions

    # ── для экранов ─────────────────────────────────────────────────────────
    def reserve_gw_links_view(self) -> list[dict]:
        """Шлюзы основы на гостевом канале для экрана роли: {slot, name, voter (на связи),
        down_since, seen (с какого момента не видит сервер — по qview)}."""
        out = []
        views: dict = {}
        if hasattr(self, "reserve_quorum_peer_line") and hasattr(self, "reserve_quorum_gw_views"):
            l = str(self.reserve_quorum_peer_line().get("l") or "")
            views = self.reserve_quorum_gw_views(l) if l else {}
            prefix = l
        else:
            prefix = ""
        known = set(int(k) for k in (self.reserve_guest_links() if hasattr(self, "reserve_guest_links") else {}) if str(k).isdigit())
        for slot in sorted(known | set(int(k) for k in self._gwv)):
            rec = self._gwv.get(slot) or {}
            online = bool(rec.get("online")) and (rec.get("lv") is None or rec["lv"].state != "down")
            w = (views.get(f"{prefix}{int(slot)}") or {}) if prefix else {}
            out.append({"slot": int(slot), "name": self.reserve_gw_name(slot), "voter": online,
                        "down_since": str(rec.get("down_since") or ""),
                        "seen": str(w.get("since") or "") if str(w.get("w") or "") == "uns" else ""})
        return out

    # ── слово state ─────────────────────────────────────────────────────────
    def _state_body(self, reason: str = "") -> dict:
        inn = self.rsv_get("rsv_in")
        serving = str(inn.get("state") or "idle") in ("taking", "serving", "releasing")   # до возврата — открыто
        body = {"state": "serving" if serving else "idle", "move": int(inn.get("gen") or 0)}
        e = self._quorum_line_epoch_for_gw()
        if e:
            body["e"] = e                                     # эпоха линии основы, как её знаю я (кворум)
        if reason:
            body["reason"] = str(reason)[:200]                 # срыв открытия после отданной точки — шлюзу Т-РА5
        return body

    def reserve_gw_state_actions(self, force: bool = False, reason: str = "") -> list[tuple]:
        """`state{serving|idle, move}` всем шлюзам основы по смене (или force)."""
        body = self._state_body(reason)
        packed = json.dumps(body, sort_keys=True)
        if not force and self.__dict__.get("_rsv_state_sent") == packed:
            return []
        self.__dict__["_rsv_state_sent"] = packed
        return [("gw", s, "state", body) for s, rec in self._gwv.items() if rec.get("online")]
