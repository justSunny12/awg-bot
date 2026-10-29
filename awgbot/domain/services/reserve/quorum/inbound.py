"""
inbound.py — часть пакета quorum (см. __init__): слова каналов о кворуме.

Канал пары: `qprobe{l, g, conf}` — мой интерфейс зонда к AWG второй линии;
`qview{l, g, w, since, e, i}` — взгляд второго сервера (на мою линию — для
карточки и ничьей; на свою — если он голосующий о клоне); `qstate{l, e,
holder, r}` — эпоха и роль держателя. Гостевой канал: `qview` шлюзов основы
о её линии (на этапе зондов — только карточка). Канал конфигурации: `qview`
своих шлюзов о моей линии. Снимок шлюза (`quo`) — взгляд и поколение, как
поле `rsv`.
"""
from __future__ import annotations

from awgbot.util import timeutil
from awgbot.util.quorum import VIEWS

from ...gateway_link.common import log


class ReserveQuorumInboundMixin:

    @property
    def _qgwviews(self) -> dict:
        """Взгляды шлюзов по линиям: узел → {w, since, e, g, at, via}."""
        return self.__dict__.setdefault("_rsv_qgwviews", {})

    # ── канал пары ──────────────────────────────────────────────────────────
    def reserve_quorum_pair_in(self, kind: str, body: dict) -> list[tuple]:
        if kind == "qprobe":
            self.reserve_qprobe_in(str(body.get("l") or ""), int(body.get("g") or 0), str(body.get("conf") or ""))
            return []
        if kind == "qview":
            w = str(body.get("w") or "")
            if w not in VIEWS:
                return []
            node = self._quorum_peer_node()
            self._qgwviews[node] = {"w": w, "since": str(body.get("since") or ""), "e": int(body.get("e") or 0),
                                    "g": int(body.get("g") or 0), "l": str(body.get("l") or ""), "at": timeutil.now_iso(),
                                    "i": dict(body.get("i") or {}), "via": "pair"}
            log.info("кворум: «%s» — %s линию %s", self.reserve_peer_name(),
                     {"see": "видит", "uns": "не видит", "sus": "сомневается", "held": "видит (держит «нет»)", "new": "ещё не смотрел"}.get(w, w),
                     body.get("l"))
            return []
        if kind == "qstate":
            self.__dict__["_quo_peer_state"] = {"l": str(body.get("l") or ""), "e": int(body.get("e") or 0),
                                                "holder": str(body.get("holder") or ""), "r": str(body.get("r") or ""),
                                                "at": timeutil.now_iso()}
            if str(body.get("l") or "") == self.reserve_quorum_side() and int(body.get("e") or 0) > self.reserve_quorum_epoch() \
                    and hasattr(self, "reserve_quorum_adopt"):
                self.reserve_quorum_adopt(int(body.get("e") or 0), f"«{self.reserve_peer_name()}» (qstate)")
            return []
        if kind == "qpause":
            v = self._qvoters.get(f"awgq{str(body.get('l') or '')}h") if hasattr(self, "_qvoters") else None
            if v is not None:
                try:
                    secs = max(0.0, timeutil.elapsed(timeutil.now(), timeutil.parse_iso(str(body.get("until") or ""))))
                except (ValueError, TypeError):
                    secs = 0.0
                import time as _t
                v.pause(_t.monotonic(), secs)
                log.info("кворум: «%s» просит паузу до %s", self.reserve_peer_name(), body.get("until"))
            return []
        if kind == "qmem":
            return self.reserve_qmem_in(body)
        if kind == "qmem_ok":
            return self.reserve_qmem_ok_in(body)
        if kind == "qkeys":
            return self.reserve_qkeys_in(body)
        if kind == "qkeys_ok":
            return self.reserve_qkeys_ok_in(body)
        return []

    def _quorum_peer_node(self) -> str:
        from awgbot.util import probenet
        side = self.reserve_quorum_side()
        return probenet.other_side(side) if side else "?"

    # ── гостевой канал (шлюзы основы о её линии) ────────────────────────────
    def reserve_quorum_guest_in_kind(self, slot: int, kind: str, body: dict) -> list[tuple]:
        """Слово шлюза основы по гостевому каналу: `qview` — взгляд; `qshare` — точка
        ключа кворума (этап решения)."""
        if kind == "qview":
            return self.reserve_quorum_guest_view_in(int(slot), body)
        if kind == "qshare" and hasattr(self, "reserve_quorum_qshare_in"):
            return self.reserve_quorum_qshare_in(int(slot), body)
        return []

    def reserve_quorum_link_in(self, slot: int, kind: str, msg: dict) -> list[tuple]:
        """Слово своего шлюза по каналу конфигурации: `qview` — взгляд на мою линию."""
        if kind == "qview":
            return self.reserve_quorum_gw_view_in(int(slot), msg)
        return []

    def reserve_quorum_guest_view_in(self, slot: int, body: dict) -> list[tuple]:
        w = str(body.get("w") or "")
        if w not in VIEWS:
            return []
        l = str(body.get("l") or "")
        node = f"{l}{int(slot)}" if l else f"?{int(slot)}"
        self._qgwviews[node] = {"w": w, "since": str(body.get("since") or ""), "e": int(body.get("e") or 0),
                                "g": int(body.get("g") or 0), "l": l, "at": timeutil.now_iso(),
                                "i": dict(body.get("i") or {}), "via": "guest"}
        log.info("кворум: шлюз %s линии %s — %s", slot, l, "видит" if w in ("see", "held") else ("не видит" if w == "uns" else w))
        return []

    # ── канал конфигурации (свои шлюзы о моей линии) ────────────────────────
    def reserve_quorum_gw_view_in(self, slot: int, body: dict) -> list[tuple]:
        w = str(body.get("w") or "")
        if w not in VIEWS:
            return []
        side = self.reserve_quorum_side()
        node = f"{side}{int(slot)}" if side else f"?{int(slot)}"
        self._qgwviews[node] = {"w": w, "since": str(body.get("since") or ""), "e": int(body.get("e") or 0),
                                "g": int(body.get("g") or 0), "l": str(body.get("l") or side), "at": timeutil.now_iso(),
                                "i": dict(body.get("i") or {}), "via": "link"}
        return []

    def reserve_quorum_snapshot_in(self, slot: int, quo: dict | None) -> None:
        """Поле `quo` снимка шлюза: взгляд на основу (моя линия) и клон."""
        if not isinstance(quo, dict):
            return
        side = self.reserve_quorum_side()
        if not side:
            return
        node = f"{side}{int(slot)}"
        w = str(quo.get("w") or "")
        if w in VIEWS:
            cur = self._qgwviews.get(node) or {}
            if cur.get("via") != "link" or cur.get("w") != w:
                self._qgwviews[node] = {"w": w, "since": str(quo.get("since") or ""), "e": int(quo.get("e") or 0),
                                        "g": int(quo.get("g") or 0), "l": side, "at": timeutil.now_iso(), "via": "snap"}

    def reserve_quorum_gw_views(self, l: str) -> dict:
        """Взгляды узлов на линию `l` (шлюзы и второй сервер), как они известны мне."""
        return {n: v for n, v in self._qgwviews.items() if str(v.get("l") or "") == l}
