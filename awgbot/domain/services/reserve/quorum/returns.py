"""
returns.py — часть пакета quorum (см. __init__): возвраты по кворуму
(концепт «кворум» §3.7.8).

«Сразу» — Q2: пока мои пользователи у резерва, голосующие (второй сервер и
мои шлюзы) продолжают пробовать мой интерфейс AWG; ответчик у меня дома
считает их «да» (роль в ответе — `follow`), и «здоров» для рычага «сразу»
— «да» строго больше половины v (вместо «свой AWG отвечает» и «шлюз на
связи»). Аварийный возврат — Q1 о боте основы на резерве: я — кандидат о
своём клоне (свой зонд `awgq<l>r`, взгляды своих шлюзов на клон по каналу
конфигурации, `tgt = r`); «не видят» > v/2 дольше T_up — забираю своих сам:
`qtake` своим шлюзам, запись на свой адрес, `deleteWebhook`, поллинг — как
С9, без условия «резерв не на связи 10 мин».
"""
from __future__ import annotations

import time

from awgbot.util import probenet, timeutil
from awgbot.util.quorum import members as qm
from awgbot.util.quorum.candidate import Candidate
from awgbot.util.quorum.probe import Info

from ...gateway_link.common import log


class ReserveQuorumReturnsMixin:

    # ── Q2: здоровье для рычага «сразу» ─────────────────────────────────────
    def reserve_quorum_q2(self) -> dict:
        """Счёт «да» за меня в отъезде: {y, v, majority}; пусто — кворума нет."""
        ld = self._qleader
        if ld is None or not getattr(ld, "q2", False):
            return {}
        now = time.monotonic()
        y = ld.yes_count(now)
        return {"y": y, "v": ld.v, "majority": qm.majority(y, ld.v)}

    def reserve_quorum_healthy(self) -> bool | None:
        """Здоров ли я глазами голосующих (Q2 > v/2); None — кворум не ведёт."""
        if not self.reserve_quorum_active() or self.reserve_out() != "away":
            return None
        q2 = self.reserve_quorum_q2()
        return bool(q2.get("majority")) if q2 else False

    # ── Q1 о клоне: аварийный возврат ───────────────────────────────────────
    @property
    def _qback(self) -> Candidate | None:
        return self.__dict__.get("_rsv_qback")

    def _quorum_back_setup(self) -> Candidate | None:
        """Кандидат о своём клоне — пока мои пользователи у резерва и кворум включён."""
        side = self.reserve_quorum_side()
        m = self.reserve_quorum_mine()
        if not side or m is None or not self.reserve_quorum_active() or self.reserve_quorum_leader_host() == side:
            self.__dict__.pop("_rsv_qback", None)
            return None
        voters = m.voters(probenet.other_side(side))          # лидер — бот основы на резерве: голосуют я и мои шлюзы
        c = self._qback
        if c is None or c.l != side:
            c = Candidate(side, len(voters), side, timing=self.reserve_quorum_timing(), voters=tuple(n for n in voters if n != side))
            self.__dict__["_rsv_qback"] = c
            log.info("кворум: голосую о своём боте на «%s» — голосующих %d", self.reserve_peer_name(), len(voters))
        elif tuple(c.voters) != tuple(n for n in voters if n != side):
            c.set_members(voters, len(voters))
        return c

    def reserve_quorum_back_tick(self, now: float | None = None) -> list[tuple]:
        now = time.monotonic() if now is None else now
        c = self._quorum_back_setup()
        if c is None:
            return []
        v = self._qvoters.get(f"awgq{c.l}r")
        c.set_own(v.view if v is not None else "new", now)
        views = self.reserve_quorum_gw_views(c.l, "r")
        for node in c.voters:
            gv = views.get(node) or {}
            try:
                _s, slot = qm.parse_node(node)
            except ValueError:
                continue
            alive = bool(self.gwlink_session(slot)) if hasattr(self, "gwlink_session") else True
            w = str(gv.get("w") or "")
            if w:
                vt = c.votes.get(node)
                if vt is None or vt.w != w or vt.since != str(gv.get("since") or ""):
                    c.view_in(node, w, now, since=str(gv.get("since") or ""), e=int(gv.get("e") or 0),
                              info=Info.from_dict(gv.get("i")) if gv.get("i") else None)
            c.channel(node, alive)
        if c.v < 2:
            c.tick(now)                                       # один голосующий (я): счёт для карточки, возврата по Q1 нет
            return []
        actions: list[tuple] = []
        for act in c.tick(now):
            if act.startswith("ask:"):
                for node in act[4:].split(","):
                    actions += self._quorum_to_own_gw(node, "qask", {"l": c.l, "g": int(self.reserve_quorum_mine().g)})
            elif act == "rel":
                e = int(v.epoch) if v is not None else 0
                pledged = c.pledged()                         # сказали «нет» — и те, кто снова видит клон
                for node in pledged:
                    actions += self._quorum_to_own_gw(node, "qrel", {"l": c.l, "e": e})
                c.unpledge(pledged)
            elif act == "take":
                actions += self._quorum_force_return(c, v)
            elif act == "tie":
                log.warning("кворум: ничья о моём боте на «%s»", self.reserve_peer_name())
        return actions

    def _quorum_to_own_gw(self, node: str, kind: str, body: dict) -> list[tuple]:
        try:
            _s, slot = qm.parse_node(node)
        except ValueError:
            return []
        return [("gwlink", slot, kind, {**body, "tgt": "r"})]

    def _quorum_force_return(self, c: Candidate, v) -> list[tuple]:
        """«Не видят» бот основы на резерве > v/2 дольше T_up: забираю своих сам."""
        from ...types import ServiceError
        if self.reserve_out() != "away":
            c.reset()
            return []
        e = int(v.epoch) if v is not None else 0
        actions: list[tuple] = []
        sent = []
        for node in c.voters:                                 # всем голосующим с живым каналом
            vt = c.votes.get(node)
            if vt is not None and vt.alive:
                actions += self._quorum_to_own_gw(node, "qtake", {"l": c.l, "e": e})
                sent.append(node)
        c.unpledge(sent)
        log.warning("кворум: бот на «%s» не видят %d из %d — возвращаю пользователей сам", self.reserve_peer_name(),
                    len(c.no_nodes()), c.v)
        self.rsv_update("rsv_out", quorum_return={"at": timeutil.now_iso(), "no": len(c.no_nodes()), "v": c.v})
        try:
            actions += self.reserve_force_home(check=False)
        except ServiceError as err:
            log.warning("кворум: аварийный возврат не пошёл: %s", err)
            c.blocked()
        return actions

    def reserve_quorum_back_view(self) -> dict:
        """Для диалога С9 (Т-К19) и карточки: сколько голосующих видят бот на резерве."""
        c = self._qback
        if c is None:
            return {}
        return {"y": len(c.see_nodes()), "no": len(c.no_nodes()), "v": c.v, "known": bool(c.see_nodes() or c.no_nodes())}

    # ── корреляция голосующих (Т-К15) ────────────────────────────────────────
    def reserve_quorum_correlation(self) -> list[tuple[list[str], str]]:
        """Голосующие моей линии, выходящие в интернет из одной сети /24: [(имена, сеть)] —
        по Endpoint линков слотов на этом сервере и адресу второго сервера."""
        import ipaddress
        m = self.reserve_quorum_mine()
        if m is None:
            return []
        addrs: dict[str, str] = {}
        peer_ip = self.reserve_peer_ip() if hasattr(self, "reserve_peer_ip") else ""
        if peer_ip:
            addrs[self.reserve_peer_name()] = peer_ip
        for node in m.gateways:
            try:
                _s, slot = qm.parse_node(node)
            except ValueError:
                continue
            if not hasattr(self, "gateway_external_ip"):
                continue
            ip = self.gateway_external_ip(slot)
            if ip:
                addrs[self._quorum_node_name(node, m.l)] = str(ip).rsplit(":", 1)[0].strip("[]")
        groups: dict[str, list[str]] = {}
        for name, ip in addrs.items():
            try:
                net = str(ipaddress.ip_network(f"{ip}/24", strict=False))
            except ValueError:
                continue
            groups.setdefault(net, []).append(name)
        return [(names, net) for net, names in groups.items() if len(names) >= 2]


__all__ = ["ReserveQuorumReturnsMixin"]
