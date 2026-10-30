"""
tie.py — часть пакета quorum (см. __init__): ничья — громкая тревога обоим
админам и громкий отбой (концепт «кворум» §3.7.5, §6.4, §7.3).

Ничья — v чётно, «да» = «нет» = v/2 без молчащих дольше T_tie. Кандидат
(правило `util/quorum/candidate`) шлёт Т-К5б своему админу и Т-К5в админу
спорного сервера в его роли (с кнопкой аварийного кода; окно кода открыто,
пока свой взгляд «не вижу»); лидер, у которого «да» ровно половина и столько
же «нет» по копиям `qview`, — Т-К5 своему админу поимённо. Одно сообщение на
эпизод каждому; ничьей нет ≥ `tie_clear_s` — Т-К6 тем же адресатам. Отметка
эпизода — `rsv_qlog.tie`.
"""
from __future__ import annotations

from awgbot.core import config
from awgbot.util import timeutil

from ...gateway_link.common import log


class ReserveQuorumTieMixin:

    # ── кандидат ────────────────────────────────────────────────────────────
    def reserve_quorum_tie(self, c, now: float) -> list[tuple]:
        """Эпизод ничьей у кандидата: Т-К5б, Т-К5в, окно кода при своём «не вижу»."""
        from awgbot.domain.notices import reserve as rn
        from ...types import Notification
        name = self.reserve_peer_name()
        since = _episode_iso(now, c.tie_since)
        see = [self._quorum_node_name(n, c.l) for n in c.see_nodes()]
        no = [self._quorum_node_name(n, c.l) for n in c.no_nodes()]
        info, said = self._quorum_tie_info(c)
        self.rsv_update("rsv_qlog", tie_cand={"episode": since, "told": True, "side": "cand", "l": c.l})
        opened = bool(self.rsv_get("rsv_peer_admin").get("opened"))
        pid = self.reserve_peer_admin_id() if hasattr(self, "reserve_peer_admin_id") else 0
        notes = [Notification(tg_id=int(config.ADMIN_ID), force_sound=True, keyboard="quorum_tie_cand",
                              text=rn.quorum_tie_candidate(name, since, see, no, info, said, bool(pid and opened)))]
        if pid and opened:
            notes.append(Notification(tg_id=pid, force_sound=True, keyboard="peer_code",
                                      text=rn.quorum_tie_peer(name, since, see, no, info, said)))
        actions: list[tuple] = [("notify", notes)]
        if c.own == "uns" and not self.reserve_code_window()["open"]:
            actions += self.reserve_code_window_quiet(True)   # окно кода — пока свой взгляд «не вижу» и ничья длится
        log.warning("кворум: ничья о «%s» с %s — видят %s, не видят %s", name, since, see, no)
        return actions

    def reserve_quorum_tie_over(self, c) -> list[tuple]:
        from awgbot.domain.notices import reserve as rn
        from ...types import Notification
        tie = dict(self.rsv_get("rsv_qlog").get("tie_cand") or {})
        if not tie.get("told"):
            return []
        name = self.reserve_peer_name()
        outcome = "moved" if self.reserve_hosting() else "stayed"
        y, v = len(c.see_nodes()), c.v
        self.rsv_update("rsv_qlog", tie_cand={**tie, "told": False, "over": timeutil.now_iso()})
        pid = self.reserve_peer_admin_id() if hasattr(self, "reserve_peer_admin_id") else 0
        notes = [Notification(tg_id=int(config.ADMIN_ID), force_sound=True, text=rn.quorum_tie_over(name, y, v, outcome))]
        if pid and bool(self.rsv_get("rsv_peer_admin").get("opened")):
            notes.append(Notification(tg_id=pid, force_sound=True, text=rn.quorum_tie_over(name, y, v, outcome)))
        actions: list[tuple] = [("notify", notes)]
        if self.reserve_code_window()["open"] and not self.__dict__.get("_quo_exec"):
            actions += self.reserve_code_window_quiet(False)
        log.info("кворум: ничьей о «%s» больше нет — %s", name, outcome)
        return actions

    def _quorum_tie_info(self, c) -> tuple[dict | None, str]:
        """Справка о трафике спорного сервера — из `qview{see, i}` видящих; (справка, чья)."""
        best, who = None, ""
        for n in c.see_nodes():
            vt = c.votes.get(n)
            if vt is None or not vt.info or not vt.info.at:
                continue
            if best is None or vt.info.at > best.at:
                best, who = vt.info, self._quorum_node_name(n, c.l)
        if best is None:
            return None, ""
        return best.to_dict(), who

    # ── лидер ───────────────────────────────────────────────────────────────
    def reserve_quorum_leader_tie_tick(self, ld, now: float) -> list[tuple]:
        """Лидер видит ничью: «да» ровно v/2 и столько же «нет» по копиям qview — дольше
        T_tie — Т-К5 поимённо; прошло — Т-К6."""
        from awgbot.domain.notices import reserve as rn
        from ...types import Notification
        if ld.v % 2:
            return []
        yes = set(ld.yes_nodes(now))
        views = self.reserve_quorum_gw_views(ld.l)
        peer_node = self._quorum_peer_node()
        no = {n for n, gv in views.items() if gv.get("w") == "uns" and n in ld.voters and n not in yes}
        if peer_node in ld.voters and peer_node not in yes and (views.get(peer_node) or {}).get("w") == "uns":
            no.add(peer_node)
        tie_now = len(yes) == ld.v // 2 and len(no) == ld.v // 2
        if "_quo_ltie" not in self.__dict__:
            tie0 = dict(self.rsv_get("rsv_qlog").get("tie_lead") or {})
            told0 = bool(tie0.get("told"))                    # после перезапуска эпизод не повторяется
            self.__dict__["_quo_ltie"] = {"since": None, "told": told0, "gone": None}
        st = self.__dict__["_quo_ltie"]
        t = self.reserve_quorum_timing()
        name = str(config.SERVER_NAME or "")
        if tie_now:
            st["gone"] = None
            if st["since"] is None:
                st["since"] = now
            if not st["told"] and now - st["since"] >= t.tie_s:
                st["told"] = True
                since = _episode_iso(now, st["since"])
                self.rsv_update("rsv_qlog", tie_lead={"episode": since, "told": True, "side": "lead", "l": ld.l})
                see_n = [self._quorum_node_name(n, ld.l) for n in sorted(yes)]
                no_n = [self._quorum_node_name(n, ld.l) for n in sorted(no)]
                info = self._quorum_info().to_dict()
                log.warning("кворум: ничья о «%s» — видят %s, не видят %s", name, see_n, no_n)
                return [("notify", [Notification(tg_id=int(config.ADMIN_ID), force_sound=True, keyboard="quorum_tie",
                                                 keyboard_arg=self.reserve_peer_name(),
                                                 text=rn.quorum_tie_leader(name, self.reserve_peer_name(), since, see_n, no_n, info))])]
            return []
        if st["told"]:
            if st["gone"] is None:
                st["gone"] = now
            if now - st["gone"] >= t.tie_clear_s:
                st.update(since=None, told=False, gone=None)
                tie = dict(self.rsv_get("rsv_qlog").get("tie_lead") or {})
                if tie.get("told"):
                    self.rsv_update("rsv_qlog", tie_lead={**tie, "told": False, "over": timeutil.now_iso()})
                outcome = "fenced" if ld.fenced else "stayed"
                return [("notify", [Notification(tg_id=int(config.ADMIN_ID), force_sound=True,
                                                 text=rn.quorum_tie_over(name, len(yes), ld.v, outcome, mine=True))])]
        else:
            st["since"] = None
        return []

    def reserve_quorum_tie_view(self, side: str = "lead") -> dict:
        """Для карточки: {since} — ничья длится; пусто — нет. `side` — чья: `lead` —
        о моём сервере (блок «📤», Т-К18), `cand` — о втором (блок «📥»)."""
        tie = dict(self.rsv_get("rsv_qlog").get("tie_cand" if side == "cand" else "tie_lead") or {})
        return {"since": str(tie.get("episode") or "")} if tie.get("told") else {}


def _episode_iso(now: float, since_mono: float | None) -> str:
    """Момент начала эпизода (монотонное время такта → ISO): «с HH:MM» — когда ничья началась, не когда о ней сказали."""
    back = max(0.0, float(now) - float(since_mono)) if since_mono is not None else 0.0
    return timeutil.to_iso(timeutil.shift(timeutil.now(), -back))


__all__ = ["ReserveQuorumTieMixin"]
