"""
screen.py — часть пакета quorum (см. __init__): данные блока голосов карточки
«🛟 Резерв» (Т-К1…Т-К3, Т-К20) и экрана роли админа второго сервера (Т-К22)
— одним словарём, без расчётов в слое бота.
"""
from __future__ import annotations

from awgbot.util import probenet
from awgbot.util.quorum import members as qm


class ReserveQuorumScreenMixin:

    def _quorum_node_name(self, node: str, line: str) -> str:
        """Имя узла в текстах: свой сервер, второй сервер, свой шлюз по имени, чужой
        шлюз — «шлюз N «Имя»» (имён второй стороне нет)."""
        from awgbot.core import config
        side = self.reserve_quorum_side()
        if node == side:
            return str(config.SERVER_NAME or "этот сервер")
        if node == probenet.other_side(side):
            return self.reserve_peer_name()
        try:
            s, slot = qm.parse_node(node)
        except ValueError:
            return node
        if s == side:
            for g in self.db.gateways():
                if int(g.id) == slot:
                    return self._gw_name(g) if hasattr(self, "_gw_name") else f"Шлюз {slot}"
            return f"Шлюз {slot}"
        return f"шлюз {slot} «{self.reserve_peer_name()}»"

    def reserve_quorum_card(self) -> dict:
        """Блок голосов карточки: {on, why, mine: {...}, peer: {...}}."""
        side = self.reserve_quorum_side()
        if not side or not self.reserve_paired():
            return {}
        why = self.reserve_quorum_off_reasons()
        m = self.reserve_quorum_mine()
        out: dict = {"on": not why, "why": why, "side": side, "peer_name": self.reserve_peer_name(),
                     "observe": not self.reserve_quorum_act(), "pending": self.reserve_quorum_pending()}
        if m is not None:
            leader_host = self.reserve_quorum_leader_host()
            mine = {"g": int(m.g), "v": m.v(leader_host), "t": m.t(leader_host), "leader_here": leader_host == side,
                    "voters": []}
            count = self.reserve_quorum_count() if leader_here(mine) else {}
            gw_views = self.reserve_quorum_gw_views(side)
            for node in m.voters(leader_host):
                seen = (count.get("voters") or {}).get(node) if count else None
                gv = gw_views.get(node) or {}
                w = (seen or {}).get("w") or gv.get("w") or ""
                age = int((seen or {}).get("age", -1)) if seen else -1
                if seen and age >= 0 and age > self.reserve_quorum_timing().yes_window_s:
                    w = w or "silent"
                mine["voters"].append({"node": node, "name": self._quorum_node_name(node, side), "w": w,
                                       "since": str(gv.get("since") or ""), "age": age, "silent": bool(seen is None or age < 0 or
                                       age > self.reserve_quorum_timing().yes_window_s)})
            if count:
                mine.update(y=int(count["y"]), role=str(count["role"]), fenced_since=str(count.get("fenced_since") or ""))
            else:
                guest = self.__dict__.get("_guest_qcount") or {}
                if guest:
                    mine.update(y=int(guest.get("y") or 0), role=str(guest.get("r") or ""), from_guest=True)
            out["mine"] = mine
        peer = self.reserve_quorum_peer_line()
        if peer:
            other = probenet.other_side(side)
            view = self.reserve_quorum_other_view()
            try:
                pm = qm.Members.make(str(peer.get("l") or other), int(peer.get("g") or 1), peer.get("members") or [])
                v = pm.v(other)
            except ValueError:
                v = 0
            gw_views = self.reserve_quorum_gw_views(other)
            out["peer"] = {"active": bool(peer.get("active")), "why": list(peer.get("why") or []), "v": v,
                           "my_w": str(view.get("w") or ""), "my_since": str(view.get("since") or ""),
                           "y": int(view.get("y") or 0), "yv": int(view.get("v") or 0), "r": str(view.get("r") or ""),
                           "gw_views": [{"node": n, "name": self._quorum_node_name(n, other), "w": str(gv.get("w") or ""),
                                         "since": str(gv.get("since") or "")} for n, gv in sorted(gw_views.items())]}
        return out


def leader_here(mine: dict) -> bool:
    return bool(mine.get("leader_here"))
