"""
keep.py — часть пакета journal (см. __init__): сторона резерва — хранение
журнала второй стороны (её линии данных).

События основы (`jev`) и её пачки счётчиков (`jcnt`, сторона a) ложатся
шифротекстом под её `K` в `reserve/jrn/` (rsvjrn.segments): бот резерва
проверяет только открытые заголовки — линия та, что у снимка, эпоха не
меньше известной, номера подряд — и подтверждает после fsync (`jack`,
`jcack`). Разрыв в номерах — `jneed` с первого недостающего; линия без
снимка — `jneed` с нуля (основа шлёт снимок); эпоха меньше известной —
`jstale`. Снимок (`pkg3`) снимает всё, что в нём учтено.

Пока у меня бот основы на резерве (гость), записи основы идут и ему — по
локальному каналу (он следует за основой до передачи бота и складывает
счётчики её оставшихся сессий); его записи и подтверждения основы —
транзитом (relay). `st.jrn.keep` — что я храню: основа сверяется по нему
до первой отправки.
"""
from __future__ import annotations

import os

from awgbot.util import timeutil

from ...gateway_link.common import log
from ..package import reserve_dir

_HOSTING = ("taking", "serving", "releasing")


class ReserveKeepMixin:

    def reserve_keep_store(self):
        from awgbot.util.rsvjrn.segments import Store
        return Store(os.path.join(reserve_dir(), "jrn"))

    def _keep(self) -> dict:
        return self.rsv_get("rsv_jrn_in")

    def reserve_keep_line(self) -> str:
        return str(self._keep().get("line") or "")

    def reserve_keep_snapshot_in(self, marks: dict) -> None:
        """Принят снимок `pkg3` с отметками журнала: новая линия — хранилище заново;
        учтённое снимком — прочь."""
        line, e, i, cs = str(marks.get("line") or ""), int(marks.get("e") or 0), int(marks.get("i") or 0), int(marks.get("cs") or 0)
        if not line:
            return
        st = self.reserve_keep_store()
        cur = self._keep()
        if str(cur.get("line") or "") != line:
            st.wipe()
            cur = {}
        dropped = st.drop_events_upto(i)
        st.drop_counts_upto("a", cs)
        last_e, last_i = st.last()
        self.rsv_put("rsv_jrn_in", {"line": line, "e": max(e, last_e, int(cur.get("e") or 0)), "snap_e": e, "snap_i": i,
                                    "cs": cs, "last": max(i, last_i), "snap_at": timeutil.now_iso()})
        if dropped:
            log.info("резерв: снимок «%s» по %d — снято событий журнала: %d", self.reserve_peer_name(), i, dropped)

    def reserve_keep_drop(self) -> None:
        """Пара снята или возврат: журнал второй стороны — прочь (его K я знал)."""
        try:
            self.reserve_keep_store().wipe()
        except OSError as e:
            log.info("резерв: журнал второй стороны не снят: %s", e)
        self.rsv_put("rsv_jrn_in", None)

    def reserve_keep_view(self) -> dict:
        """Что храню о линии второй стороны — в `st.jrn.keep`."""
        cur = self._keep()
        if not cur.get("line"):
            return {}
        st = self.reserve_keep_store()
        last = int(cur.get("last") or 0)
        return {"line": str(cur["line"]), "e": int(cur.get("e") or 0), "last": last, "snap_i": int(cur.get("snap_i") or 0),
                "snap_n": max(0, last - int(cur.get("snap_i") or 0)), "size": st.events_size()}

    def _keep_guest(self) -> bool:
        """Гость основы у меня поднят — её записи нужны и ему."""
        return str(self.rsv_get("rsv_in").get("state") or "idle") in _HOSTING

    # ── приём от основы ─────────────────────────────────────────────────────
    def _keep_jev_in(self, body: dict) -> list[tuple]:
        from awgbot.util.rsvjrn import event
        cur = self._keep()
        line = str(body["line"])
        if not cur.get("line") or line != str(cur["line"]):
            return [("send", "jneed", {"line": line, "from": 0})]
        try:
            recs = event.split(bytes.fromhex(str(body["blob"])))
            heads = [event.head(r) for r in recs]
        except (ValueError, event.RecordError) as e:
            log.warning("резерв: события «%s» не разобрались: %s", self.reserve_peer_name(), e)
            return [("send", "jneed", {"line": line, "from": int(cur.get("last") or 0) + 1})]
        e_body, known = int(body["e"]), int(cur.get("e") or 0)
        if not heads or any(h.kind != "E" or h.line != line or h.e != e_body for h in heads) or \
                [h.n for h in heads] != list(range(int(body["from"]), int(body["from"]) + len(heads))):
            log.warning("резерв: события «%s» с заголовками не по порядку — отказ", self.reserve_peer_name())
            return [("send", "jneed", {"line": line, "from": int(cur.get("last") or 0) + 1})]
        if e_body < known:
            return [("send", "jstale", {"line": line, "e": known})]
        last = int(cur.get("last") or 0)
        new = [r for r, h in zip(recs, heads) if h.n > last]
        if new and event.head(new[0]).n != last + 1:
            return [("send", "jneed", {"line": line, "from": last + 1})]
        acts: list[tuple] = []
        if new:
            self.reserve_keep_store().append_events(new)       # fsync — до подтверждения
            last = event.head(new[-1]).n
            self.rsv_update("rsv_jrn_in", e=e_body, last=last)
            if self._keep_guest():
                acts.append(("guest", "jev", {"line": line, "e": e_body, "from": event.head(new[0]).n,
                                              "n": len(new), "blob": b"".join(new).hex()}))
        return [("send", "jack", {"line": line, "e": e_body, "i": last})] + acts

    def _keep_jcnt_in(self, body: dict) -> list[tuple]:
        from awgbot.util.rsvjrn import event
        cur = self._keep()
        line, side = str(body["line"]), str(body["side"])
        if side != "a" or not cur.get("line") or line != str(cur["line"]):
            return []
        try:
            raw = bytes.fromhex(str(body["blob"]))
            h = event.head(raw)
        except (ValueError, event.RecordError):
            return []
        if h.kind != "a" or h.line != line or h.n != int(body["s"]) or h.size != len(raw):
            return []
        st = self.reserve_keep_store()
        acts: list[tuple] = []
        if h.n > st.counts_last("a") and h.n > int(cur.get("cs") or 0):
            st.append_counts("a", [raw])
            if self._keep_guest():
                acts.append(("guest", "jcnt", {"line": line, "side": "a", "s": h.n, "blob": raw.hex()}))
        return [("send", "jcack", {"line": line, "side": "a", "s": h.n})] + acts
