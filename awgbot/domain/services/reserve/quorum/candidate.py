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
        off = self.reserve_quorum_peer_timing_off()
        if off:
            # цифры кандидата и лидера связаны (T_up ≥ W + T_down + …): по разным цифрам запас может уйти в минус —
            # два писателя; кворум о второй линии не включаю, окно кода — запасным сигналом по линку пары
            if self.__dict__.get("_quo_tm_told") != off:
                self.__dict__["_quo_tm_told"] = off
                log.warning("кворум: цифры кворума расходятся с «%s» (%s) — голосование о нём не включаю",
                            self.reserve_peer_name(), off)
            self.__dict__.pop("_rsv_qcand", None)
            return None
        self.__dict__.pop("_quo_tm_told", None)
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
            tie0 = dict(self.rsv_get("rsv_qlog").get("tie_cand") or {})
            c.tie_told = bool(tie0.get("told"))               # после перезапуска эпизод не повторяется
            self.__dict__["_rsv_qcand"] = c
            log.info("кворум: кандидат о линии «%s» — голосующих %d", self.reserve_peer_name(), len(voters))
        elif tuple(c.voters) != tuple(n for n in voters if n != side) or c.v != len(voters):
            c.set_members(voters, len(voters))
        return c

    def reserve_quorum_peer_timing_off(self) -> str:
        """Цифры времени из `st.quo` второй стороны расходятся с моими: какие (через запятую);
        пусто — сходятся или она их не прислала."""
        tm = self.reserve_quorum_peer_line().get("tm")
        if not isinstance(tm, dict) or not tm or not hasattr(self, "reserve_quorum_timing_digits"):
            return ""
        mine = self.reserve_quorum_timing_digits()
        try:
            return ",".join(k for k, v in mine.items() if int(tm.get(k) or 0) != int(v))
        except (TypeError, ValueError):
            return ",".join(mine)

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
            alive = self._quorum_gw_channel_alive(slot)
            w = str(gv.get("w") or "")
            if w:
                vt = c.votes.get(node)
                if vt is None or vt.w != w or vt.since != str(gv.get("since") or ""):
                    info = Info.from_dict(gv.get("i")) if gv.get("i") else None
                    c.view_in(node, w, now, since=str(gv.get("since") or ""), e=int(gv.get("e") or 0), info=info)
            c.channel(node, alive)

    def _quorum_gw_channel_alive(self, slot: int) -> bool:
        """Канал шлюза второй линии жив: сессия гостевого канала открыта и живость не «down».
        Без учёта гостевого канала (тесты правила) — считаем живым."""
        if not hasattr(self, "_gwv"):
            return True
        rec = self._gwv.get(slot) or {}
        if not rec.get("online"):
            return False
        lv = rec.get("lv")
        return lv is None or lv.state != "down"

    # ── такт ────────────────────────────────────────────────────────────────
    def reserve_quorum_candidate_tick(self, now: float | None = None) -> list[tuple]:
        now = time.monotonic() if now is None else now
        took = self._quorum_took_tick() + self._quorum_code_settle()
        c = self._quorum_candidate_setup()
        if c is None:
            self.__dict__.pop("_quo_exec", None)
            return took
        self._quorum_candidate_feed(c, now)
        if c.v < 2:
            # голосующий один (я): автоматического взятия, опроса и ничьей нет — только окно кода
            # по своему «не вижу» (Р4); исполнение прежнего состава снимается
            c.tick(now)
            c.decided = False                                 # «забираю» при одном голосующем не решение — флаг не держим
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
        return took + actions

    def _quorum_code_settle(self) -> list[tuple]:
        """Пользователи второй стороны у меня: окно кода, открытое кандидатом или по линку пары,
        закрыто (автоматический переезд — Т-РН3 «голосов хватило», кнопка — тихо); отметка ввода
        кода (`pending`) после состоявшегося переезда прочь — иначе окно не открылось бы снова."""
        inn = self.reserve_in()
        if inn not in ("taking", "serving", "releasing"):
            return []
        cw = dict(self.rsv_get("rsv_view").get("code") or {})
        if cw.get("open"):
            auto = str(self.rsv_get("rsv_in").get("kind") or "") in ("dead", "rf")
            return self.reserve_code_window_set(False, by="taken", quiet=not auto)
        if cw.get("pending") and inn != "taking":             # в taking исход кода ещё впереди (reserve_code_failed)
            cw["pending"] = False
            self.rsv_update("rsv_view", code=cw)
        return []

    def _quorum_took_tick(self) -> list[tuple]:
        """Исход отданного `qtake`: переезд состоялся (serving, releasing) — эпоха e + 1 у шлюзов
        верна, память прочь; открытие идёт или исполнение ещё живо — ждать; иначе (срыв
        открытия, кандидата больше нет) — `qrel{e}` тем, кому ушёл `qtake`."""
        took = self.__dict__.get("_quo_took")
        if not took:
            return []
        inn = self.reserve_in()
        if inn in ("serving", "releasing"):
            self.__dict__.pop("_quo_took", None)
            return []
        if inn == "taking" or self.__dict__.get("_quo_exec"):
            return []
        return self._quorum_took_release()

    def _quorum_took_release(self) -> list[tuple]:
        """Исполнение снято после отданного `qtake`: `qrel{e}` под той же эпохой тем же узлам —
        шлюзы откатывают поднятую им эпоху (исход не случился)."""
        took = self.__dict__.pop("_quo_took", None) or {}
        out: list[tuple] = []
        for node in took.get("nodes") or []:
            out += self._quorum_to_gw(node, "qrel", {"l": str(took.get("l") or ""), "e": int(took.get("e") or 0)})
        if out:
            log.info("кворум: «забираю» под эпоху %s не исполнено — qrel %s", int(took.get("e") or 0) + 1,
                     ",".join(took.get("nodes") or []))
        return out

    def _quorum_code_watch(self, c: Candidate) -> list[tuple]:
        """Окно кода вне решения: свой взгляд «не вижу», никто из голосующих не видит
        (молчат или не видят), большинства «нет» нет — голосов не хватает (§3.6, Т-К21);
        свой «вижу» или чей-то «вижу» — окно закрывается. Окно решения (`_quo_exec`)
        и ничьей ведут свои правила."""
        if self.__dict__.get("_quo_exec") or c.tie_told:
            return []
        cw = dict(self.rsv_get("rsv_view").get("code") or {})
        maj = qm.majority(len(c.no_nodes()), c.v) if c.v >= 2 else False   # при v = 1 большинства как решения нет
        want = c.own == "uns" and not c.see_nodes() and not maj and self.reserve_zone_free()   # relay — зона свободна
        if want and not cw.get("open"):
            why = self.reserve_open_block()
            if why:
                # код ничего не откроет (готовность, пакет, ящик кода, DNS вручную) — окна и «введите код» нет;
                # причину карточка показывает своими строками (готовность, направление «к себе»)
                if self.__dict__.get("_quo_watch_block") != why:
                    self.__dict__["_quo_watch_block"] = why
                    log.warning("кворум: «%s» не вижу, голосов не хватает, а приём кода не открыт: %s", self.reserve_peer_name(), why)
                return []
            self.__dict__.pop("_quo_watch_block", None)
            return self.reserve_code_window_set(True, since=self._quorum_no_since(c), n=len(c.no_nodes()), v=c.v, t=qm.threshold(c.v))
        if not want and cw.get("open") and cw.get("by") == "quorum" and (maj or c.see_nodes()):
            # закрывает только факт: кто-то видит или «нет» набрало большинство; свой «ещё не смотрел»
            # (перезапуск бота: голосующий заново, каналы шлюзов не поднялись) — не повод, окно ждёт взгляда;
            # голосов хватило — переезд идёт сам; при v = 1 — второй сервер снова видит; иначе — большинство
            return self.reserve_code_window_set(False, by="taken" if maj else ("seen" if c.v < 2 else "quorum"))
        return []

    def _quorum_known_e(self, c: Candidate) -> int:
        """Эпоха её линии по фактам: `st.jrn.mine.e` второй стороны и ответы её ответчика — не
        поднятая собой после «забираю» (повторный `qtake` после срыва идёт под той же эпохой,
        гость стартует с эпохой пакета + 1 — шлюзы не уходят вперёд него)."""
        v = self._qvoters.get(f"awgq{c.l}h")
        known = int(self._quorum_known_epoch(c.l)) if hasattr(self, "_quorum_known_epoch") else 0
        return max(known, int(v.fact) if v is not None else 0)

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
        took = list((self.__dict__.get("_quo_took") or {}).get("nodes") or [])
        for node in pledged:
            if node not in took:
                out += self._quorum_to_gw(node, "qrel", {"l": c.l, "e": e})
        out += self._quorum_took_release()
        c.unpledge(pledged)
        self.__dict__.pop("_quo_exec", None)
        cw = dict(self.rsv_get("rsv_view").get("code") or {})
        if cw.get("open") and cw.get("by") == "quorum":
            out += self.reserve_code_window_set(False, by="quorum")   # окно кода решения — закрыть
        return out

    # ── исполнение ──────────────────────────────────────────────────────────
    def _quorum_take(self, c: Candidate, now: float) -> list[tuple]:
        why = self.reserve_auto_block()
        if why:
            log.warning("кворум: «нет» о «%s» большинством, но открытие не идёт: %s", self.reserve_peer_name(), why)
            self.rsv_update("rsv_view", auto_block=why)
            if c.own == "uns" and not self.reserve_open_block():
                # мешают только паузы автоматического переезда (демпфер после срыва, интервал между
                # переездами) — код от них не зависит: окно кода (Т-К21) сразу, «забираю» — когда пауза кончится
                since = self._quorum_no_since(c)
                self.__dict__["_quo_exec"] = {"stage": "code", "at": now, "e": self._quorum_known_e(c), "since": since,
                                              "points": {}, "need_at": 0.0, "own": c.own, "paused": True}
                if not self.reserve_code_window()["open"]:
                    return self.reserve_code_window_set(True, since=since, n=len(c.no_nodes()), v=c.v, t=qm.threshold(c.v))
                return []
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
        if sent:
            # кому ушёл qtake — до исхода: снятие исполнения без переезда отдаст им qrel{e}
            prev = self.__dict__.get("_quo_took") or {}
            if prev and (int(prev.get("e") or 0) != e or str(prev.get("l") or "") != c.l):
                actions = self._quorum_took_release() + actions   # прежнее «забираю» другой эпохи — снять
                prev = {}
            self.__dict__["_quo_took"] = {"l": c.l, "e": e, "nodes": sorted(set(prev.get("nodes") or []) | set(sent))}
        v = self._qvoters.get(f"awgq{c.l}h")
        if v is not None:
            v.raise_to(e + 1)                                 # исход знаю сам: прежнему лидеру под e больше не «да»
        since = self._quorum_no_since(c)
        self.__dict__["_quo_exec"] = {"stage": "ask", "at": now, "e": e, "since": since, "points": {}, "need_at": 0.0, "own": c.own}
        log.warning("кворум: забираю «%s» под эпоху %s: «нет» %d из %d", self.reserve_peer_name(), e + 1, len(c.no_nodes()), c.v)
        if self._quorum_peer_reachable():
            actions.append(("send", "ask", {"case": "q", "since": since, "kind": self._quorum_kind(c)}))
            self.__dict__["_quo_exec"]["asked"] = True
        else:
            self.__dict__["_quo_exec"]["stage"] = "points"
            actions += self._quorum_need(c, since)
        return actions

    def _quorum_peer_reachable(self) -> bool:
        """Второй сервер на связи живым словом: сеанс канала есть и живость не `down`. Один флаг
        сеанса — не то: сеанс отрезанного сервера держится до ~23 мин, и просьба ждала бы
        `ask_seconds` впустую вместо пути по точкам."""
        if not self.reserve_online():
            return False
        try:
            return self.reserve_peer_seen() != "down"
        except Exception:                                     # noqa: BLE001
            return True

    @staticmethod
    def _quorum_kind(c: Candidate) -> str:
        """Вид переезда (Р6): свой взгляд «не вижу» — сервер мёртв (`dead`); «вижу», а шлюзы нет —
        случай 3 (`rf`): тексты называют, кому он не отвечает."""
        return "dead" if c.own == "uns" else "rf"

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
                                           self._quorum_kind(c), "q")

    def _quorum_exec_tick(self, c: Candidate, now: float) -> list[tuple]:
        """Ход исполнения: просьба без ответа → точки; точек не хватает → окно кода."""
        ex = self.__dict__.get("_quo_exec")
        if not ex:
            return []
        if self.reserve_hosting():
            self.__dict__.pop("_quo_exec", None)             # пошло открытие (просьба или точки) — счёт кандидата заново
            c.reset()
            return []
        stage = str(ex.get("stage") or "")
        if stage == "opened":
            # открытие по точкам не пошло (предусловия к моменту точек, отказ запуска, срыв раньше
            # этого такта): решение снято, счёт заново — демпфер, повтор и окно кода обычным путём
            self.__dict__.pop("_quo_exec", None)
            c.reset()
            log.info("кворум: открытие «%s» по точкам не состоялось — решение снято", self.reserve_peer_name())
            return self._quorum_took_release()
        if c.own != "uns" and str(ex.get("own") or "") == "uns" and stage in ("ask", "points", "code"):
            # решение было при своём «не вижу», а взгляд вернулся: исполнение снято, окно кода закрыто, счёт
            # заново (случай 3 — своим длинным таймером; его исполнение при своём «вижу» идёт по точкам шлюзов)
            self.__dict__.pop("_quo_exec", None)
            c.reset()
            out = self._quorum_rel(c)
            log.info("кворум: «%s» снова вижу — исполнение решения снято", self.reserve_peer_name())
            return out
        if stage == "code" and ex.get("paused") and not self.reserve_auto_block():
            # пауза кончилась: исполнение того же решения — просьба, точки; окно кода остаётся открытым
            log.info("кворум: пауза автоматического переезда кончилась — исполняю решение о «%s»", self.reserve_peer_name())
            return self._quorum_take(c, now)
        ask_s = settings.get_int("app.reserve.ask_seconds", 60)
        if stage == "ask" and ex.get("asked"):
            leaving = str(self.reserve_peer_state().get("out") or "") == "leaving"
            if leaving and not ex.get("agreed_at"):
                # ответа на ask нет, но лидер уже отдаёт пользователей путём кнопки (st.out = leaving): согласие —
                # ждать его go_req (свежий пакет и разовый ключ), а не идти по точкам параллельно
                ex["agreed_at"] = now
                log.info("кворум: «%s» отдаёт пользователей по просьбе — жду его go_req", self.reserve_peer_name())
            if ex.get("agreed_at"):
                from ..outbound import EPH_WAIT_S, PKG_WAIT_S
                if leaving and now - float(ex["agreed_at"]) < PKG_WAIT_S + EPH_WAIT_S:
                    return []
                ex["stage"] = "points"
                log.warning("кворум: «%s» согласился, но переезд кнопкой не дошёл до меня — по точкам", self.reserve_peer_name())
                return self._quorum_need(c, str(ex.get("since") or ""))
        if stage == "ask" and now - float(ex.get("at") or now) >= ask_s:
            if 0 < int(ex.get("busy") or 0) < 3 and self._quorum_peer_reachable():
                # лидер ответил «занят» (плановая пауза) — повторить просьбу (§3.6), не сразу по точкам
                ex["at"] = now
                log.info("кворум: «%s» был занят — прошу снова (%d)", self.reserve_peer_name(), int(ex["busy"]))
                return [("send", "ask", {"case": "q", "since": str(ex.get("since") or ""), "kind": self._quorum_kind(c)})]
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
