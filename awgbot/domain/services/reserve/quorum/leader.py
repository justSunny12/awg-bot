"""
leader.py — часть пакета quorum (см. __init__): ответчик и счёт «да» у лидера
своей линии — правило `util/quorum/leader`.

Лидер — основа дома (ответчик на втором адресе интерфейса AWG) или бот
основы на резерве (ответчик на втором адресе клона в зоне; состав и v ему
говорит бот резерва по локальному каналу — `quorum{l, g, members}`, он
отвечает `qcount{y, v, r}`). Проба — JSON внутри туннеля зонда; ответчик
сверяет узел с адресом источника, считает «да» только со своей эпохой и
действующим поколением, в ответ кладёт роль, счёт и справку (устройств с
рукопожатием за 5 мин без зондов, идёт ли трафик). Лишение кворума и возврат
— `fence` (`jrn_fence`/`jrn_unfence`); ничья — `tie`. В отъезде ответчик дома
продолжает считать «да» (роль `follow`) — это Q2 для возврата «сразу»
(`returns`); решений в отъезде нет.
"""
from __future__ import annotations

import time

from awgbot.core import config
from awgbot.util import probenet, timeutil
from awgbot.util.quorum import members as qm
from awgbot.util.quorum.leader import Leader
from awgbot.util.quorum.probe import Info, Probe

from ...gateway_link.common import log

INFO_EVERY_S = 30                # дамп интерфейса для справки — не чаще
HS_FRESH_S = 300                 # устройство «на связи» — рукопожатие за столько


class ReserveQuorumLeaderMixin:

    @property
    def _qleader(self) -> Leader | None:
        return self.__dict__.get("_rsv_qleader")

    def _quorum_leader_setup(self) -> Leader | None:
        """Машина лидера по текущему составу; None — я не лидер своей линии
        (пары нет, кворум не включён, пользователи у резерва)."""
        q2 = False
        if getattr(self, "guest", False):
            q = self.__dict__.get("_guest_quorum") or {}
            l, g, members = str(q.get("l") or ""), int(q.get("g") or 0), list(q.get("members") or [])
            if not l or not members or not q.get("active", True):
                # состава нет или линия основы выключена (цифры, блок зондов): второго писателя по кворуму
                # быть не может, ограждения по голосам нет (аварийного возврата по Q1 у выключенной линии нет)
                self.__dict__.pop("_rsv_qleader", None)
                return None
            leader_host = probenet.other_side(l)              # клон живёт у второй стороны
        else:
            m = self.reserve_quorum_mine()
            if m is None or not self.reserve_quorum_active():
                self.__dict__.pop("_rsv_qleader", None)
                return None
            q2 = self.reserve_quorum_leader_host() != m.l        # отъезд: ответчик дома считает «да» для Q2
            if q2 and not self._quorum_away_for_q2():
                self.__dict__.pop("_rsv_qleader", None)
                return None
            l, g, members, leader_host = m.l, int(m.g), list(m.members), m.l
        try:
            comp = qm.Members.make(l, g, members)
            voters = comp.voters(leader_host)
        except ValueError:
            self.__dict__.pop("_rsv_qleader", None)
            return None
        ld = self._qleader
        now = time.monotonic()
        if ld is None or ld.l != l or ld.q2 != q2:
            ld = Leader(l, len(voters), self.reserve_quorum_epoch(), candidate=probenet.other_side(leader_host),
                        timing=self.reserve_quorum_timing(), voters=voters, q2=q2)
            ld.gen.bump(g, now, ld.timing.grace_s)
            self.__dict__["_rsv_qleader"] = ld
            log.info("кворум: ответчик линии %s%s — голосующих %d, поколение %s", l, " (отъезд, счёт для возврата)" if q2 else "",
                     len(voters), g)
            self.__dict__["_quo_start_pending"] = not q2 and len(voters) >= 2   # старт — без права записи до первого большинства; при v = 1 решений нет
            self.__dict__["_quo_ld_at"] = now
        else:
            if tuple(ld.voters) != tuple(voters) or ld.gen.g != g:
                ld.set_members(voters, len(voters), g, now)
            ld.set_epoch(self.reserve_quorum_epoch())
        return ld

    def reserve_quorum_leader_start(self) -> None:
        """Старт процесса основы (до поллинга): лидер своей линии дома — без права записи, пока
        правило не наберёт первое большинство «да». Ограждение ставится здесь, а не первым тактом
        кворума: между стартом и тактом правка была бы принята, хотя эпоху мог забрать кандидат,
        пока основа лежала."""
        if getattr(self, "guest", False):
            return
        ld = self._quorum_leader_setup()
        if ld is None or ld.q2 or ld.v < 2:
            return
        if self.__dict__.pop("_quo_start_pending", False) and hasattr(self, "reserve_quorum_fence_apply"):
            self.reserve_quorum_fence_apply("start", ld)      # действий у «старта» нет (Т-К7 — по сроку, тактом)

    def _quorum_away_for_q2(self) -> bool:
        """Пользователи уходят или ушли к резерву: `away`, шаги передачи `leaving` (record/release/
        released — шлюзам уже сказано «away», их пробы домой должны получать ответ, иначе «не вижу» и
        обязательство «нет» закрыли бы возврат «сразу» по Q2 до срока обязательства) и возврат до
        `relay`."""
        state, step = self.reserve_out(), str(self.rsv_get("rsv_out").get("step") or "")
        return state == "away" or (state == "leaving" and step in ("record", "release", "released")) \
            or (state == "returning" and step != "relay")

    def reserve_quorum_responder_addr(self) -> tuple[str, int] | None:
        """(адрес, порт) ответчика, если я лидер своей линии; иначе None."""
        from awgbot.infra import quoprobe
        if getattr(self, "guest", False):
            q = self.__dict__.get("_guest_quorum") or {}
            if not q.get("l") or not q.get("members") or not q.get("active", True):
                return None                                   # линия основы выключена — сокета без ответов не держим
            return probenet.responder_addr(str(q["l"]), clone=True), int(q.get("port") or quoprobe.port())
        m = self.reserve_quorum_mine()
        if m is None or not self.reserve_quorum_active():
            return None
        if self.reserve_quorum_leader_host() != m.l and not self._quorum_away_for_q2():
            return None
        return probenet.responder_addr(m.l), int(self.reserve_probes().get(m.l, {}).get("port") or quoprobe.port())

    # ── проба ───────────────────────────────────────────────────────────────
    def reserve_quorum_probe_in(self, data: bytes, src: str) -> bytes | None:
        """Датаграмма пробы с адреса `src` → ответ (или None: не проба, чужой узел,
        я не лидер)."""
        ld = self._quorum_leader_setup()
        if ld is None:
            return None
        p = Probe.parse(data)
        if p is None or p.l != ld.l:
            return None
        if probenet.node_of_addr(ld.l, src) != p.n:
            log.info("кворум: проба узла %s с адреса %s — не его адрес, не считаю", p.n, src)
            return None
        now = time.monotonic()
        act = ld.probe_in(p, now)
        if act == "ignore":
            return None
        if act == "adopt" and not ld.q2:                       # Q2 (отъезд): эпоха пробы не сверяется, журнал не трогаем
            if not self.__dict__.get("_quo_adopt_told"):
                self.__dict__["_quo_adopt_told"] = True
                log.warning("кворум: проба %s несёт эпоху %s, у меня %s — эпоху держит другая сторона", p.n, p.e, ld.epoch)
            if hasattr(self, "reserve_quorum_adopt"):
                self.reserve_quorum_adopt(int(p.e), f"пробы {p.n}")
                ld.set_epoch(self.reserve_quorum_epoch())
        return ld.reply_for(p, now, self._quorum_info()).encode()

    def _quorum_info(self) -> Info:
        """Справка в ответ пробы: устройств с рукопожатием за 5 мин (без зондов), рос ли
        приём за минуту; дамп — не чаще раза в 30 с."""
        from awgbot.infra import awg
        cache = self.__dict__.setdefault("_quo_info", {})
        now = time.monotonic()
        if cache.get("at") and now - cache["at"] < INFO_EVERY_S:
            return cache["info"]
        hs, rx = 0, 0
        try:
            for p in awg.show_dump(config.AWG_INTERFACE):
                if probenet.is_probe(str(p.get("address") or "")):
                    continue
                if p.get("last_handshake") and time.time() - int(p["last_handshake"]) <= HS_FRESH_S:
                    hs += 1
                rx += int(p.get("rx") or 0)
        except Exception as e:                                # noqa: BLE001
            log.info("кворум: справка не снята: %s", e)
        prev_rx, prev_at = cache.get("rx"), cache.get("rx_at")
        flow = bool(prev_rx is not None and rx > prev_rx and now - float(prev_at or now) <= 90)
        info = Info(hs=hs, flow=flow, at=timeutil.now_iso())
        cache.update(at=now, info=info, rx=rx, rx_at=now)
        return info

    # ── такт ────────────────────────────────────────────────────────────────
    def reserve_quorum_leader_tick(self, now: float | None = None) -> list[tuple]:
        """Такт лидера: счёт «да», лишение кворума и возврат (на этапе зондов — только
        журнал и карточка). Действия — для исполнителя runtime."""
        ld = self._quorum_leader_setup()
        if ld is None:
            if hasattr(self, "reserve_quorum_unfence_if_off"):
                self.reserve_quorum_unfence_if_off()
            return []
        now = time.monotonic() if now is None else now
        if ld.q2:
            self.__dict__.pop("_quo_start_pending", None)     # отъезд: счёт для Q2, решений нет
            return []
        if ld.v < 2:
            # один голосующий (второй сервер): счёт для карточки, ограждения нет — судьбу сервера не
            # решает один голос (Р4); ограждение прежнего состава снимается
            self.__dict__.pop("_quo_start_pending", None)
            ld.tick(now)
            if ld.fenced:
                ld.fenced, ld.starting, ld.high_since = False, False, None
            if self.__dict__.get("_quo_fenced") and hasattr(self, "reserve_quorum_unfence_if_off"):
                self.reserve_quorum_unfence_if_off()
            return []
        actions: list[tuple] = []
        if self.__dict__.pop("_quo_start_pending", False) and hasattr(self, "reserve_quorum_fence_apply"):
            actions += self.reserve_quorum_fence_apply("start", ld)
        act = ld.tick(now)
        y = ld.yes_count(now)
        if act == "fence":
            log.warning("кворум: «%s» без кворума — да %d из %d (%s)", self._quorum_my_name(), y, ld.v, self._quorum_views_brief(ld, now))
            self.__dict__["_quo_fenced_since"] = timeutil.now_iso()
        elif act == "unfence":
            log.info("кворум: «%s» — лидер, да %d из %d", self._quorum_my_name(), y, ld.v)
            self.__dict__["_quo_fenced_since"] = ""
        if act in ("fence", "unfence") and hasattr(self, "reserve_quorum_fence_apply"):
            actions += self.reserve_quorum_fence_apply(act, ld)
        if not act and ld.fenced and ld.starting and now - float(self.__dict__.get("_quo_ld_at") or now) >= ld.timing.down_s \
                and not (self.rsv_get("rsv_qlog").get("fenced") or {}).get("told") and hasattr(self, "reserve_quorum_fence_apply"):
            actions += self.reserve_quorum_fence_apply("fence", ld)   # старт без большинства дольше T_down — Т-К7
            self.__dict__["_quo_fenced_since"] = str((self.rsv_get("rsv_qlog").get("fenced") or {}).get("since") or "")
        if hasattr(self, "reserve_quorum_leader_tie_tick"):
            actions += self.reserve_quorum_leader_tie_tick(ld, now)
        return actions

    def _quorum_my_name(self) -> str:
        return str(config.SERVER_NAME or self.reserve_quorum_side() or "")

    @staticmethod
    def _quorum_views_brief(ld: Leader, now: float) -> str:
        parts = []
        for n in ld.voters:
            s = ld.seen.get(n)
            if s is None or now - s.at > ld.timing.yes_window_s:
                parts.append(f"{n} молчит")
            else:
                parts.append(f"{n} {s.w}")
        return ", ".join(parts)

    def reserve_quorum_count(self) -> dict:
        """Счёт лидера для карточки и `qcount`: {y, v, t, role, fenced_since, voters:
        {узел: {w, e, g, age}}}; пусто — я не лидер."""
        ld = self._qleader
        if ld is None:
            return {}
        now = time.monotonic()
        voters = {}
        for n in ld.voters:
            s = ld.seen.get(n)
            voters[n] = ({"w": s.w, "e": s.e, "g": s.g, "age": int(now - s.at)} if s is not None else {"w": "", "age": -1})
        return {"l": ld.l, "y": ld.yes_count(now), "v": ld.v, "t": qm.threshold(ld.v), "role": ld.role(),
                "fenced_since": str(self.__dict__.get("_quo_fenced_since")
                                    or ((self.rsv_get("rsv_qlog").get("fenced") or {}).get("since") if ld.fenced else "") or ""),
                "voters": voters}

    # ── бот основы на резерве ───────────────────────────────────────────────
    def reserve_quorum_guest_phase(self) -> str:
        """Состояние кворума у бота основы на резерве: `waiting` — стартовал, состава от бота
        резерва ещё нет (решений и записи нет); `ready` — состав есть, решает правило лидера;
        пусто — не гость."""
        if not getattr(self, "guest", False):
            return ""
        return "ready" if self.__dict__.get("_guest_quorum") else "waiting"

    def reserve_quorum_guest_start(self) -> None:
        """Старт бота основы на резерве: состав приходит от бота резерва по локальному каналу
        первым словом сессии, а до него гость не знает, не возвращает ли основа пользователей
        прямо сейчас (аварийный возврат по Q1) — без права записи, пока состав не пришёл и правило
        лидера не набрало большинство «да». Состояние `waiting` снимает только состав."""
        if self.reserve_quorum_guest_phase() != "waiting":
            return
        meta = self.db.jrn_fence(why="кворум: старт гостя — жду состав")
        if meta is None and not self._quorum_jrn_fenced():
            return
        self.__dict__["_quo_fenced"] = True
        fenced = dict(self.rsv_get("rsv_qlog").get("fenced") or {})
        if not fenced.get("on"):
            fenced = {"on": True, "since": timeutil.now_iso(), "told": False, "start": True}
        self.rsv_update("rsv_qlog", fenced={**fenced, "on": True, "hold": True})
        log.info("кворум: гость стартовал без состава — жду его от бота резерва, без права записи")

    def reserve_quorum_guest_in(self, body: dict) -> None:
        """`quorum{l, g, members, port}` от бота резерва по локальному каналу — состав
        для счёта «да» на клоне; состояние `waiting` кончается, дальше — правило лидера
        (старт без права записи до первого большинства «да»)."""
        first = self.reserve_quorum_guest_phase() == "waiting"
        l = str(body.get("l") or "")
        members = [str(m) for m in (body.get("members") or []) if isinstance(m, str)][:64]
        if l not in ("a", "b") or not members:
            log.warning("кворум: слово quorum без линии или состава — состава нет, ответчик снят, жду состав")
            self.__dict__.pop("_guest_quorum", None)
            return
        self.__dict__["_guest_quorum"] = {"l": l, "g": int(body.get("g") or 0), "members": members,
                                          "port": int(body.get("port") or 0), "active": bool(body.get("active", True))}
        fenced = dict(self.rsv_get("rsv_qlog").get("fenced") or {})
        if fenced.get("hold"):
            self.rsv_update("rsv_qlog", fenced={k: v for k, v in fenced.items() if k != "hold"})
        if first:
            log.info("кворум: состав от бота резерва пришёл — правило лидера")

    def reserve_quorum_qcount_body(self) -> dict | None:
        """`qcount{y, v, r}` гость → бот резерва; None — счёта нет."""
        c = self.reserve_quorum_count()
        if not c:
            return None
        return {"y": int(c["y"]), "v": int(c["v"]), "r": str(c["role"])}

    def reserve_quorum_for_guest(self) -> dict | None:
        """Сторона бота резерва: состав линии основы для её бота у меня — по `st.quo`
        второй стороны (её линия) и моей записи зондов; None — нечего."""
        from awgbot.infra import quoprobe
        peer = self.reserve_quorum_peer_line()
        if not peer.get("l") or not peer.get("members"):
            return None
        return {"l": str(peer["l"]), "g": int(peer.get("g") or 0), "members": list(peer["members"]), "port": quoprobe.port(),
                "active": bool(peer.get("active"))}            # линия выключена — гость не ограждается по голосам

    def reserve_quorum_qcount_in(self, body: dict) -> None:
        self.__dict__["_guest_qcount"] = {"y": int(body.get("y") or 0), "v": int(body.get("v") or 0),
                                          "r": str(body.get("r") or ""), "at": timeutil.now_iso()}
