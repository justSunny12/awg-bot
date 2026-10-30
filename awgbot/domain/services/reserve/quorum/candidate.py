"""
candidate.py — часть пакета quorum (см. __init__): правило кандидата о
линии второй стороны — правило `util/quorum/candidate` и исполнение решения
(концепт «кворум» §3.5, §3.6).

Я — кандидат о линии второй стороны, пока её пользователи у неё (её лидер —
она сама), а у меня зона свободна. Свой взгляд — мой зонд к её интерфейсу
AWG (`awgq<l>h`), взгляды её шлюзов — `qview` по гостевому каналу (живые —
пока сессия канала открыта). Опрос — `qask`, снятие — `qrel`, решение —
`qtake` всем «не видящим», затем первый возможный путь: по просьбе (`ask{q}`
лидеру по каналу пары — он отвечает путём кнопки), по точкам (`qneed` →
`qshare`, t точек → процесс открытия через ящик кворума), по коду (окно
кода, когда свой взгляд «не вижу», а голосов или точек не хватает).
Предусловия открытия (`reserve_auto_block`) — без изменений.
"""
from __future__ import annotations

import time

from awgbot.core import settings
from awgbot.util import probenet, rsvkeys, timeutil
from awgbot.util.quorum import members as qm
from awgbot.util.quorum.candidate import Candidate
from awgbot.util.quorum.probe import Info

from ...gateway_link.common import log
from ..opening import NEED_RETRY_S


class ReserveQuorumCandidateMixin:

    @property
    def _qcand(self) -> Candidate | None:
        return self.__dict__.get("_rsv_qcand")

    def _quorum_candidate_setup(self) -> Candidate | None:
        """Машина кандидата по составу линии второй стороны; None — я не кандидат
        (пары нет, её кворум не включён, её пользователи у меня, у меня нет зонда)."""
        side = self.reserve_quorum_side()
        peer = self.reserve_quorum_peer_line()
        if not side or not peer.get("active") or not peer.get("members") or self.reserve_hosting():
            self.__dict__.pop("_rsv_qcand", None)
            return None
        other = probenet.other_side(side)
        try:
            comp = qm.Members.make(str(peer.get("l") or other), int(peer.get("g") or 1), peer.get("members") or [])
            voters = comp.voters(other)                       # лидер её линии — она сама (дома)
        except ValueError:
            self.__dict__.pop("_rsv_qcand", None)
            return None
        if side not in voters:
            self.__dict__.pop("_rsv_qcand", None)
            return None
        c = self._qcand
        if c is None or c.l != other:
            c = Candidate(other, len(voters), side, timing=self.reserve_quorum_timing(), voters=tuple(n for n in voters if n != side))
            tie0 = dict(self.rsv_get("rsv_qlog").get("tie") or {})
            c.tie_told = bool(tie0.get("told")) and tie0.get("side") == "cand"   # после перезапуска эпизод не повторяется
            self.__dict__["_rsv_qcand"] = c
            log.info("кворум: кандидат о линии «%s» — голосующих %d", self.reserve_peer_name(), len(voters))
        elif tuple(c.voters) != tuple(n for n in voters if n != side) or c.v != len(voters):
            c.set_members(voters, len(voters))
        return c

    def _quorum_candidate_feed(self, c: Candidate, now: float) -> None:
        """Свой взгляд — из зонда к её AWG; взгляды шлюзов — из `qview` гостевого канала,
        живость — сессия канала."""
        v = self._qvoters.get(f"awgq{c.l}h")
        c.set_own(v.view if v is not None else "new", now)
        views = self.reserve_quorum_gw_views(c.l)
        for node in c.voters:
            gv = views.get(node) or {}
            try:
                _s, slot = qm.parse_node(node)
            except ValueError:
                continue
            rec = (self._gwv.get(slot) or {}) if hasattr(self, "_gwv") else {}
            alive = bool(rec.get("online")) and (rec.get("lv") is None or rec["lv"].state != "down") if rec or hasattr(self, "_gwv") else True
            w = str(gv.get("w") or "")
            if w:
                vt = c.votes.get(node)
                if vt is None or vt.w != w or vt.since != str(gv.get("since") or ""):
                    info = Info.from_dict(gv.get("i")) if gv.get("i") else None
                    c.view_in(node, w, now, since=str(gv.get("since") or ""), e=int(gv.get("e") or 0), info=info)
            c.channel(node, alive)

    # ── такт ────────────────────────────────────────────────────────────────
    def reserve_quorum_candidate_tick(self, now: float | None = None) -> list[tuple]:
        now = time.monotonic() if now is None else now
        c = self._quorum_candidate_setup()
        if c is None:
            self.__dict__.pop("_quo_exec", None)
            return []
        self._quorum_candidate_feed(c, now)
        if c.v < 2:
            # голосующий один (я): автоматического взятия, опроса и ничьей нет — только окно кода
            # по своему «не вижу» (Р4); исполнение прежнего состава снимается
            c.tick(now)
            self.__dict__.pop("_quo_exec", None)
            return self._quorum_code_watch(c)
        actions: list[tuple] = []
        for act in c.tick(now):
            if act.startswith("ask:"):
                g = int(self.reserve_quorum_peer_line().get("g") or 0)
                for node in act[4:].split(","):
                    actions += self._quorum_to_gw(node, "qask", {"l": c.l, "g": g})
                log.info("кворум: опрос о «%s» — %s", self.reserve_peer_name(), act[4:])
            elif act == "rel":
                actions += self._quorum_rel(c)
            elif act == "take":
                actions += self._quorum_take(c, now)
            elif act == "tie":
                actions += self.reserve_quorum_tie(c, now)
            elif act == "tie_over":
                actions += self.reserve_quorum_tie_over(c)
        actions += self._quorum_exec_tick(c, now)
        actions += self._quorum_code_watch(c)
        return actions

    def _quorum_code_watch(self, c: Candidate) -> list[tuple]:
        """Окно кода вне решения: свой взгляд «не вижу», никто из голосующих не видит
        (молчат или не видят), большинства «нет» нет — голосов не хватает (§3.6, Т-К21);
        свой «вижу» или чей-то «вижу» — окно закрывается. Окно решения (`_quo_exec`)
        и ничьей ведут свои правила."""
        if self.__dict__.get("_quo_exec") or c.tie_told:
            return []
        cw = dict(self.rsv_get("rsv_view").get("code") or {})
        maj = qm.majority(len(c.no_nodes()), c.v) if c.v >= 2 else False   # при v = 1 большинства как решения нет
        want = c.own == "uns" and not c.see_nodes() and not maj and self.reserve_in() == "idle"
        if want and not cw.get("open"):
            return self.reserve_code_window_set(True, since=self._quorum_no_since(c), n=len(c.no_nodes()), v=c.v, t=qm.threshold(c.v))
        if not want and cw.get("open") and cw.get("by") == "quorum":
            return self.reserve_code_window_set(False)
        return []

    def _quorum_known_e(self, c: Candidate) -> int:
        v = self._qvoters.get(f"awgq{c.l}h")
        return int(v.epoch) if v is not None else 0

    def _quorum_to_gw(self, node: str, kind: str, body: dict) -> list[tuple]:
        try:
            _s, slot = qm.parse_node(node)
        except ValueError:
            return []
        return [("gw", slot, kind, body)]

    def _quorum_rel(self, c: Candidate) -> list[tuple]:
        """«Нет» ушло ниже половины или открытие не пускают: `qrel` всем, кто сказал
        «нет» — обязательство держит и тот, кто снова видит лидера."""
        e = self._quorum_known_e(c)
        out: list[tuple] = []
        pledged = c.pledged()
        for node in pledged:
            out += self._quorum_to_gw(node, "qrel", {"l": c.l, "e": e})
        c.unpledge(pledged)
        self.__dict__.pop("_quo_exec", None)
        if self.reserve_code_window()["open"] and hasattr(self, "_code_window"):
            out += self._code_window("6")                     # окно кода — закрыть
        return out

    # ── исполнение ──────────────────────────────────────────────────────────
    def _quorum_take(self, c: Candidate, now: float) -> list[tuple]:
        why = self.reserve_auto_block()
        if why:
            log.warning("кворум: «нет» о «%s» большинством, но открытие не идёт: %s", self.reserve_peer_name(), why)
            self.rsv_update("rsv_view", auto_block=why)
            c.blocked()
            return self._quorum_rel(c)
        if self.rsv_get("rsv_view").get("auto_block"):
            self.rsv_update("rsv_view", auto_block="")
        e = self._quorum_known_e(c)
        actions: list[tuple] = []
        sent = []
        for node in c.voters:                                 # qtake{e} — всем голосующим с живым каналом (§3.6)
            vt = c.votes.get(node)
            if vt is not None and vt.alive:
                actions += self._quorum_to_gw(node, "qtake", {"l": c.l, "e": e})
                sent.append(node)
        c.unpledge(sent)                                      # qtake снимает обязательство на шлюзе
        v = self._qvoters.get(f"awgq{c.l}h")
        if v is not None:
            v.epoch_in(e + 1)                                 # исход знаю сам: прежнему лидеру под e больше не «да»
        since = self._quorum_no_since(c)
        self.__dict__["_quo_exec"] = {"stage": "ask", "at": now, "e": e, "since": since, "points": {}, "need_at": 0.0, "own": c.own}
        log.warning("кворум: забираю «%s» под эпоху %s: «нет» %d из %d", self.reserve_peer_name(), e + 1, len(c.no_nodes()), c.v)
        if self.reserve_online():
            actions.append(("send", "ask", {"case": "q", "since": since}))
            self.__dict__["_quo_exec"]["asked"] = True
        else:
            self.__dict__["_quo_exec"]["stage"] = "points"
            actions += self._quorum_need(c, since)
        return actions

    def _quorum_no_since(self, c: Candidate) -> str:
        views = self.rsv_get("rsv_view").get("quo_views") or {}
        own = (views.get(f"awgq{c.l}h") or {}).get("since") or ""
        return str(own or timeutil.now_iso())

    def _quorum_need(self, c: Candidate, since: str) -> list[tuple]:
        peer = self.reserve_quorum_peer_keys()
        q_gen = int(peer.get("q_gen") or 0)
        ex = self.__dict__.get("_quo_exec") or {}
        ex["need_at"] = time.monotonic()
        actions: list[tuple] = []
        for node in c.voters:
            vt = c.votes.get(node)
            if vt is not None and vt.w == "uns":
                actions += self._quorum_to_gw(node, "qneed", {"l": c.l, "q_gen": q_gen, "since": since})
        log.warning("кворум: прошу точки ключа кворума «%s» (поколение %s)", self.reserve_peer_name(), q_gen)
        return actions

    def reserve_quorum_qshare_in(self, slot: int, body: dict) -> list[tuple]:
        """`qshare` шлюза второй линии: точка многочлена — в сбор, хватает t — открытие."""
        ex = self.__dict__.get("_quo_exec")
        c = self._qcand
        if not ex or ex.get("stage") != "points" or c is None:
            log.info("кворум: точка от шлюза %s без просьбы — не беру", slot)
            return []
        peer = self.reserve_quorum_peer_keys()
        if int(body.get("q_gen") or 0) != int(peer.get("q_gen") or 0):
            log.warning("кворум: точка шлюза %s поколения %s, у меня %s", slot, body.get("q_gen"), peer.get("q_gen"))
            return []
        try:
            x, y = int(body.get("x") or 0), str(body.get("y") or "")
            if x != rsvkeys.gateway_x(int(slot)) or len(bytes.fromhex(y)) != rsvkeys.M_LEN:
                raise ValueError("не та точка")
        except (ValueError, TypeError) as e:
            log.warning("кворум: точка шлюза %s не принята: %s", slot, e)
            return []
        ex["points"][str(x)] = y
        return self._quorum_try_open(c)

    def _quorum_try_open(self, c: Candidate) -> list[tuple]:
        ex = self.__dict__.get("_quo_exec") or {}
        peer = self.reserve_quorum_peer_keys()
        t = int(peer.get("t") or 0)
        points = {int(k): v for k, v in (ex.get("points") or {}).items()}
        if c.own == "uns" and peer.get("y"):
            points[int(peer.get("x") or rsvkeys.RESERVE_X)] = str(peer["y"])   # своя точка — только при своём «не вижу»
        if not t or len(points) < t or not peer.get("qbox"):
            return []
        ex["stage"] = "opened"
        self.rsv_update("rsv_view", moved_by={"quorum": True, "points": sorted(points), "at": timeutil.now_iso()})
        log.warning("кворум: точек %d из %d — процесс открытия «%s» по ящику кворума", len(points), t, self.reserve_peer_name())
        return self.reserve_open_by_points({"qpoints": [[x, y] for x, y in sorted(points.items())], "qbox": str(peer["qbox"])},
                                           "dead", "q")

    def _quorum_exec_tick(self, c: Candidate, now: float) -> list[tuple]:
        """Ход исполнения: просьба без ответа → точки; точек не хватает → окно кода."""
        ex = self.__dict__.get("_quo_exec")
        if not ex:
            return []
        if self.reserve_in() != "idle":
            self.__dict__.pop("_quo_exec", None)             # пошло открытие (просьба или точки) — счёт кандидата заново
            c.reset()
            return []
        stage = str(ex.get("stage") or "")
        if c.own != "uns" and str(ex.get("own") or "") == "uns" and stage in ("ask", "points", "code"):
            # решение было при своём «не вижу», а взгляд вернулся: исполнение снято, окно кода закрыто, счёт
            # заново (случай 3 — своим длинным таймером; его исполнение при своём «вижу» идёт по точкам шлюзов)
            self.__dict__.pop("_quo_exec", None)
            c.reset()
            out = self._quorum_rel(c)
            log.info("кворум: «%s» снова вижу — исполнение решения снято", self.reserve_peer_name())
            return out
        ask_s = settings.get_int("app.reserve.ask_seconds", 60)
        if stage == "ask" and now - float(ex.get("at") or now) >= ask_s:
            if 0 < int(ex.get("busy") or 0) < 3 and self.reserve_online():
                # лидер ответил «занят» (плановая пауза) — повторить просьбу (§3.6), не сразу по точкам
                ex["at"] = now
                log.info("кворум: «%s» был занят — прошу снова (%d)", self.reserve_peer_name(), int(ex["busy"]))
                return [("send", "ask", {"case": "q", "since": str(ex.get("since") or "")})]
            ex["stage"] = "points"
            log.warning("кворум: «%s» не ответил на просьбу за %d с — по точкам", self.reserve_peer_name(), ask_s)
            return self._quorum_need(c, str(ex.get("since") or ""))
        if stage == "points":
            actions = self._quorum_try_open(c)
            if actions:
                return actions
            if now - float(ex.get("need_at") or now) >= 2 * NEED_RETRY_S:
                if c.own == "uns" and not self.reserve_code_window()["open"]:
                    ex["stage"] = "code"
                    log.warning("кворум: точек не хватает — приём аварийного кода открыт")
                    return self.reserve_code_window_set(True, since=str(ex.get("since") or ""), n=len(c.no_nodes()), v=c.v,
                                                        t=qm.threshold(c.v))
                ex["need_at"] = now
                return self._quorum_need(c, str(ex.get("since") or ""))
        return []

    def reserve_quorum_candidate_view(self) -> dict:
        """Состояние кандидата для карточки: {no, see, v, deadline_at, stage, own}."""
        c = self._qcand
        if c is None:
            return {}
        dl = c.take_deadline()
        now = time.monotonic()
        left = int(dl - now) if dl is not None and dl > now else 0
        return {"no": len(c.no_nodes()), "see": len(c.see_nodes()), "silent": len(c.silent_nodes()), "v": c.v, "own": c.own,
                "deadline_in": left, "deadline_at": timeutil.to_iso(timeutil.shift(timeutil.now(), left)) if left else "",
                "stage": str((self.__dict__.get("_quo_exec") or {}).get("stage") or ""), "tie": c.tie_told}
