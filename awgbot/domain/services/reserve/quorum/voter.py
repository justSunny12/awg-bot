"""
voter.py — часть пакета quorum (см. __init__): свои пробы как голосующего —
о второй линии (её интерфейс AWG, зонд `awgq<l>h`) и о своём клоне в зоне
второго сервера (`awgq<l>r`, пока мои пользователи там) — правило
`util/quorum/voter`.

Проба уходит из пространства `awg-quo` сокетом, созданным там
(runtime/quorum), с джиттером на каждую; ответ — взгляд `see`; нет ответа —
`sus`, затем `uns`. Смена взгляда — строка журнала, карточка и `qview`
второй стороне по каналу пары (копия лидеру и, в отъезде, кандидату). На
этапе зондов «нет» решения не ведёт.
"""
from __future__ import annotations

import time

from awgbot.util import probenet, timeutil
from awgbot.util.quorum import timing as qt
from awgbot.util.quorum.probe import Probe, Reply
from awgbot.util.quorum.voter import Voter

from ...gateway_link.common import log


class ReserveQuorumVoterMixin:

    @property
    def _qvoters(self) -> dict:
        return self.__dict__.setdefault("_rsv_qvoters", {})

    def _quorum_my_node(self) -> str:
        return self.reserve_quorum_side()

    def reserve_quorum_voter_tick(self, now: float | None = None) -> list[tuple]:
        """Такт голосующего по всем своим зондам: ("probe", имя, байты, (адрес, порт)),
        ("send", "qview", тело) по смене взгляда."""
        now = time.monotonic() if now is None else now
        want = self.reserve_quorum_probe_ifaces()
        me = self._quorum_my_node()
        if not me:
            self._qvoters.clear()
            return []
        for name in list(self._qvoters):
            if name not in want:
                self._qvoters.pop(name, None)
        actions: list[tuple] = []
        timing = self.reserve_quorum_timing()
        for name, rec in want.items():
            v = self._qvoters.get(name)
            if v is None:
                v = Voter(str(rec["l"]), me, timing=timing)
                v.start(now)
                self._qvoters[name] = v
            line_e = self._quorum_known_epoch(str(rec["l"]))
            if line_e > int(v.epoch):
                v.epoch_in(line_e)                            # эпоха линии (журнал, st.jrn второй стороны) — на каждом такте
            for act in v.tick(now):
                if act == "probe":
                    g = self._quorum_line_gen(str(rec["l"]))
                    p = Probe(str(rec["l"]), g or 1, me, int(v.epoch), v.w_for_probe(), v.probe_out(now, qt.jitter(timing.probe_s)))
                    actions.append(("probe", name, p.encode(), (str(rec["resp"]), int(rec["port"]))))
                elif act in ("see", "uns", "sus"):
                    actions += self._quorum_view_changed(name, rec, v, act)
        return actions

    def reserve_quorum_reply_in(self, name: str, data: bytes) -> list[tuple]:
        """Ответ ответчика на пробу зонда `name`."""
        v = self._qvoters.get(name)
        if v is None:
            return []
        r = Reply.parse(data)
        if r is None:
            return []
        now = time.monotonic()
        actions: list[tuple] = []
        for act in v.reply_in(r, now):
            if act == "see":
                rec = self.reserve_quorum_probe_ifaces().get(name) or {"l": v.l, "target": name[-1:]}
                actions += self._quorum_view_changed(name, rec, v, "see")
            elif act.startswith("adopt:"):
                log.info("кворум: ответ линии %s несёт эпоху %s — запомнил", v.l, act[6:])
        return actions

    def _quorum_view_changed(self, name: str, rec: dict, v: Voter, w: str) -> list[tuple]:
        target = "клон" if rec.get("target") == "r" else "сервер"
        log.info("кворум: %s линии %s — %s", target, rec.get("l"), {"see": "вижу", "sus": "ответ пропущен", "uns": "не вижу"}[w])
        views = dict(self.rsv_get("rsv_view").get("quo_views") or {})
        views[name] = {"w": w, "since": timeutil.now_iso(), "l": str(rec.get("l") or ""), "target": str(rec.get("target") or "")}
        self.rsv_update("rsv_view", quo_views=views)
        if w == "sus" or not self.reserve_online():
            return []
        body = {"l": str(rec.get("l") or ""), "g": int(self._quorum_line_gen(str(rec.get("l") or "")) or 0), "w": w,
                "since": views[name]["since"], "e": int(v.epoch)}
        info = v.info()
        if info is not None and w == "see":
            body["i"] = info.to_dict()
        return [("send", "qview", body)]

    def _quorum_line_gen(self, l: str) -> int:
        """Поколение состава линии: своя — из `rsv_quorum`, чужая — из `st.quo`."""
        if l == self.reserve_quorum_side():
            m = self.reserve_quorum_mine()
            return int(m.g) if m is not None else 0
        return int(self.reserve_quorum_peer_line().get("g") or 0)

    def _quorum_known_epoch(self, l: str) -> int:
        """Известная эпоха линии: своей — из журнала; чужой — из `st.jrn.mine` второй стороны."""
        if l == self.reserve_quorum_side():
            return self.reserve_quorum_epoch()
        jrn = self.reserve_peer_state().get("jrn_mine") if isinstance(self.reserve_peer_state().get("jrn_mine"), dict) else {}
        try:
            return int((jrn or {}).get("e") or 0)
        except (TypeError, ValueError):
            return 0

    def reserve_quorum_my_views(self) -> dict:
        """Мои взгляды по зондам (для карточки): имя → {w, since, l, target, y, v, r}."""
        out = {}
        views = dict(self.rsv_get("rsv_view").get("quo_views") or {})
        for name, v in self._qvoters.items():
            rec = dict(views.get(name) or {})
            rec.update(w=v.view, since=rec.get("since") or "")
            if v.last is not None:
                rec.update(y=int(v.last.y), v=int(v.last.v), r=str(v.last.r), e=int(v.last.e))
            out[name] = rec
        return out

    def reserve_quorum_other_view(self) -> dict:
        """Мой взгляд на сервер второй линии: {w, since, y, v, r} или пусто."""
        side = self.reserve_quorum_side()
        if not side:
            return {}
        return self.reserve_quorum_my_views().get(f"awgq{probenet.other_side(side)}h") or {}
