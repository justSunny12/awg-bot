"""
members.py — часть пакета quorum (см. __init__): состав своей линии и
поколение, сторона линии, цифры времени, причины «кворум не включён», поле
`quo` в `st`.

Состав линии ведёт её основа (`rsv_quorum.mine`): себя, второй сервер и свои
шлюзы с возможностью `quo1`; поколение `g` растёт на каждой смене. В 4.2.0
наблюдение (этап зондов) подтверждения второго сервера не ждёт — состав
действует сразу (`acked = g`); обмен `qmem`/`qmem_ok` — этап состава.
Сторона линии — роль в линке пары: пригласивший — `a`, принявший — `b`.
"""
from __future__ import annotations

from awgbot.core import config, settings
from awgbot.util import probenet, timeutil
from awgbot.util.quorum import members as qm
from awgbot.util.quorum.timing import Timing

from ...gateway_link.common import log
from ..state import _LOCK


class ReserveQuorumMembersMixin:

    # ── сторона и настройки ─────────────────────────────────────────────────
    def reserve_quorum_side(self) -> str:
        """Сторона своей линии: `a` — пригласивший, `b` — принявший; пусто — пары нет."""
        role = self.reserve_link_role() if hasattr(self, "reserve_link_role") else ""
        return {"inviter": "a", "acceptor": "b"}.get(role, "")

    @staticmethod
    def reserve_quorum_settings() -> dict:
        q = settings.get("app.reserve.quorum", {}) or {}
        return dict(q) if isinstance(q, dict) else {}

    def reserve_quorum_timing(self) -> Timing:
        return Timing.from_settings(self.reserve_quorum_settings(), settings.get_int("app.reserve.rf_block_minutes", 15))

    def reserve_quorum_act(self) -> bool:
        """Скрытый ключ `app.reserve.quorum.act`: решения по кворуму (лишение кворума,
        переезд) — только при true; наблюдение и счёт идут всегда."""
        return bool(self.reserve_quorum_settings().get("act", True))

    def reserve_quorum_peer_quo1(self) -> bool:
        return "quo1" in (self.reserve_peer_state().get("caps") or [])

    def _quorum_gw_nodes(self, side: str) -> tuple[list[str], list[int]]:
        """(узлы шлюзов с агентом quo1, слоты выше предела блока)."""
        nodes, big = [], []
        for gw in self.db.gateways():
            caps = self.gwlink_agent_caps(gw.id) if hasattr(self, "gwlink_agent_caps") else []
            if "quo1" not in (caps or []):
                continue
            if int(gw.id) > probenet.MAX_SLOT:
                big.append(int(gw.id))
                continue
            nodes.append(qm.gateway_node(side, int(gw.id)))
        return nodes, big

    # ── состав ──────────────────────────────────────────────────────────────
    def reserve_quorum_mine(self) -> qm.Members | None:
        mine = self.rsv_get("rsv_quorum").get("mine") or {}
        if not mine.get("members"):
            return None
        try:
            return qm.Members.from_dict(mine)
        except ValueError:
            return None

    def reserve_quorum_refresh(self) -> qm.Members | None:
        """Состав своей линии по фактам (пара, возможности): сменился — новое
        поколение. None — пары нет."""
        side = self.reserve_quorum_side()
        if not side or not self.reserve_paired():
            if self.rsv_get("rsv_quorum"):
                self.rsv_put("rsv_quorum", None)
            return None
        nodes, _big = self._quorum_gw_nodes(side)
        want = (side, probenet.other_side(side), *sorted(nodes, key=lambda n: int(n[1:])))
        with _LOCK:
            cur = self.reserve_quorum_mine()
            if cur is not None and cur.members == want:
                return cur
            g = (int(cur.g) + 1) if cur is not None else 1
            m = qm.Members.make(side, g, want)
            self.rsv_update("rsv_quorum", mine={**m.to_dict(), "acked": g, "since": timeutil.now_iso()})
        log.info("кворум: состав «%s» %s → %s: %s", str(config.SERVER_NAME or side),
                 cur.g if cur is not None else "—", g, ", ".join(want))
        if hasattr(self, "reserve_st_sent_reset"):
            self.reserve_st_sent_reset()                      # st второй стороне — с новым составом
        return m

    def reserve_quorum_peer_line(self) -> dict:
        """Линия второй стороны из её `st.quo`: {l, g, members, active, why}."""
        quo = self.reserve_peer_state().get("quo")
        return dict(quo) if isinstance(quo, dict) else {}

    # ── включение ───────────────────────────────────────────────────────────
    def _quorum_overlaps(self) -> list[str]:
        """Подсети, которые пересекают блок зондов: пользователей (основная и окно переезда
        профилей), пул зоны и veth зоны, локальные подсети шлюзов."""
        import ipaddress
        cidrs: list[str] = []
        try:
            cidrs += [str(n) for n in config.routing_client_subnets()]
        except Exception:                                     # noqa: BLE001
            pass
        for key, default in (("app.reserve.zone_pool", "169.254.96.0/19"), ("app.reserve.zone_cidr", "169.254.77.0/30"),
                             ("app.reserve.zone_rf_cidr", "169.254.77.4/30")):
            cidrs.append(str(settings.get(key, default) or default))
        try:
            from awgbot.util import lanspace
            cidrs.append(str(lanspace.space()))
        except Exception:                                     # noqa: BLE001
            pass
        out = []
        for c in cidrs:
            try:
                net = ipaddress.ip_network(str(c).split("#", 1)[0].strip(), strict=False)
            except ValueError:
                continue
            if net.version == 4 and net.overlaps(probenet.PROBE_NET):
                out.append(str(net))
        return out

    def reserve_quorum_off_reasons(self) -> list[str]:
        """Почему кворум своей линии не включён — кодами (пусто — включён)."""
        side = self.reserve_quorum_side()
        if not side:
            return ["no_pair"]
        m = self.reserve_quorum_mine()
        _nodes, big = self._quorum_gw_nodes(side)
        return qm.off_reasons(m, peer_quo1=self.reserve_quorum_peer_quo1(), acked=True,
                              timing_problem=self.reserve_quorum_timing().check(), overlaps=self._quorum_overlaps(),
                              big_slots=big)

    def reserve_quorum_active(self) -> bool:
        return not self.reserve_quorum_off_reasons()

    def reserve_quorum_leader_host(self) -> str:
        """Хост-лидер своей линии: своя сторона дома; в отъезде (пользователи у резерва)
        — другая (бот основы на резерве)."""
        side = self.reserve_quorum_side()
        if not side:
            return ""
        away = self.reserve_gw_state() == "away" if hasattr(self, "reserve_gw_state") else False
        return probenet.other_side(side) if away else side

    def reserve_quorum_st_part(self) -> dict:
        """Поле `quo` в `st` второй стороне: своя линия, поколение, состав, включён ли."""
        m = self.reserve_quorum_mine()
        if m is None:
            return {}
        why = self.reserve_quorum_off_reasons()
        return {"l": m.l, "g": int(m.g), "members": list(m.members), "active": not why, "why": why[:8]}

    def reserve_quorum_epoch(self) -> int:
        """Эпоха своей линии (журнал); без журнала — 0."""
        try:
            return int(self.db.jrn_meta()["epoch"])
        except Exception:                                     # noqa: BLE001
            return 0
