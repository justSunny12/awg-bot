"""
members.py — часть пакета quorum (см. __init__): состав своей линии и
поколение, подтверждение второго сервера (`qmem`/`qmem_ok`), сторона линии,
цифры времени, причины «кворум не включён», поле `quo` в `st`.

Состав линии ведёт её основа (`rsv_quorum.mine` — предложенный, `active` —
действующий): себя, второй сервер и свои шлюзы с возможностью `quo1`;
поколение `g` растёт на каждой смене. Новое поколение действует с
подтверждения второго сервера (`qmem_ok` — проверкой формы, кодом); пока его
нет, действует прежнее (Т-К14), а второй сервер не на связи — новое не
наступает: состав нельзя сменить в одностороннем порядке. Второй сервер
хранит состав чужой линии в `rsv_quorum.peer`. Сторона линии — роль в линке
пары: пригласивший — `a`, принявший — `b`.
"""
from __future__ import annotations

import time

from awgbot.core import config, settings
from awgbot.util import probenet, timeutil
from awgbot.util.quorum import members as qm
from awgbot.util.quorum.timing import Timing

from ...gateway_link.common import log
from ..state import _LOCK

QMEM_RESEND_S = 30


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
    @staticmethod
    def _members_of(d: dict | None) -> qm.Members | None:
        if not d or not d.get("members"):
            return None
        try:
            return qm.Members.from_dict(d)
        except ValueError:
            return None

    def reserve_quorum_proposed(self) -> qm.Members | None:
        """Предложенный состав (последний по фактам) — по нему ключи и конфиги зондов."""
        return self._members_of(self.rsv_get("rsv_quorum").get("mine"))

    def reserve_quorum_mine(self) -> qm.Members | None:
        """Действующий состав — подтверждённый второй стороной; по нему счёт и решения."""
        return self._members_of(self.rsv_get("rsv_quorum").get("active"))

    def reserve_quorum_pending(self) -> bool:
        """Предложенный состав ждёт подтверждения второго сервера (Т-К14)."""
        q = self.rsv_get("rsv_quorum")
        mine, active = q.get("mine") or {}, q.get("active") or {}
        return bool(mine.get("members")) and int(mine.get("g") or 0) != int(active.get("g") or 0)

    def reserve_quorum_refresh(self) -> qm.Members | None:
        """Состав своей линии по фактам (пара, возможности): сменился — новое
        поколение предложено; смена только когда пользователи дома и хода нет.
        Возвращает предложенный состав; None — пары нет."""
        side = self.reserve_quorum_side()
        if not side or not self.reserve_paired():
            if self.rsv_get("rsv_quorum"):
                self.rsv_put("rsv_quorum", None)
            return None
        nodes, _big = self._quorum_gw_nodes(side)
        want = (side, probenet.other_side(side), *sorted(nodes, key=lambda n: int(n[1:])))
        with _LOCK:
            cur = self.reserve_quorum_proposed()
            if cur is not None and cur.members == want:
                return cur
            fleet = self.fleet_running() if hasattr(self, "fleet_running") else False
            if cur is not None and (self.reserve_busy() or self.reserve_out() != "home" or fleet):
                return cur                                    # в отъезде, в ходе и в наборе обновлений состав не меняется
            g = (int(cur.g) + 1) if cur is not None else 1
            m = qm.Members.make(side, g, want)
            self.rsv_update("rsv_quorum", mine={**m.to_dict(), "since": timeutil.now_iso()})
            self.__dict__["_qmem_sent"] = None
        log.info("кворум: состав «%s» %s → %s: %s — жду подтверждения «%s»", str(config.SERVER_NAME or side),
                 cur.g if cur is not None else "—", g, ", ".join(want), self.reserve_peer_name())
        if hasattr(self, "reserve_st_sent_reset"):
            self.reserve_st_sent_reset()                      # st второй стороне — с новым составом
        return m

    # ── qmem / qmem_ok ──────────────────────────────────────────────────────
    def reserve_qmem_body(self) -> dict | None:
        """`qmem` второму серверу: предложенный состав, пока не подтверждён; повтор не
        чаще QMEM_RESEND_S."""
        m = self.reserve_quorum_proposed()
        if m is None or not self.reserve_quorum_pending():
            return None
        now = time.monotonic()
        sent = self.__dict__.get("_qmem_sent")
        if sent and sent[0] == int(m.g) and now - sent[1] < QMEM_RESEND_S:
            return None
        self.__dict__["_qmem_sent"] = (int(m.g), now)
        return m.to_dict()

    def reserve_qmem_reset(self) -> None:
        """Новая сессия пары — состав второй стороне заново."""
        self.__dict__["_qmem_sent"] = None

    def reserve_qmem_in(self, body: dict) -> list[tuple]:
        """`qmem` от основы второй линии: её состав — ко мне (проверка формы), ответ `qmem_ok`."""
        try:
            m = qm.Members.from_dict(body)
        except ValueError as e:
            log.warning("кворум: состав второй линии не принят: %s", e)
            return []
        if m.l == self.reserve_quorum_side():
            return []
        with _LOCK:
            self.rsv_update("rsv_quorum", peer={**m.to_dict(), "at": timeutil.now_iso()})
        log.info("кворум: состав линии «%s» %s: %s", self.reserve_peer_name(), m.g, ", ".join(m.members))
        self.__dict__.pop("_quo_guest_sent", None)            # боту основы у меня — новый состав
        return [("send", "qmem_ok", {"l": m.l, "g": int(m.g)})]

    def reserve_qmem_ok_in(self, body: dict) -> list[tuple]:
        """`qmem_ok` второго сервера: предложенное поколение действует. Поколение больше
        моего (я восстановлен из копии) — новое предложение поверх него."""
        side = self.reserve_quorum_side()
        if str(body.get("l") or "") != side:
            return []
        g = int(body.get("g") or 0)
        with _LOCK:
            q = self.rsv_get("rsv_quorum")
            mine = q.get("mine") or {}
            if int(mine.get("g") or 0) == g and mine.get("members"):
                active = {k: mine[k] for k in ("l", "g", "members")}
                self.rsv_update("rsv_quorum", active={**active, "acked_at": timeutil.now_iso()})
                log.info("кворум: состав «%s» %s подтверждён «%s»", str(config.SERVER_NAME or side), g, self.reserve_peer_name())
                self.__dict__["_quo_peers_sync_g"] = None
            elif g > int(mine.get("g") or 0) and mine.get("members"):
                m = qm.Members.make(side, g + 1, mine["members"])
                self.rsv_update("rsv_quorum", mine={**m.to_dict(), "since": timeutil.now_iso()})
                self.__dict__["_qmem_sent"] = None
                log.warning("кворум: у «%s» состав поколения %s, у меня %s — предлагаю %s", self.reserve_peer_name(), g,
                            mine.get("g"), g + 1)
        return []

    def reserve_quorum_peer_line(self) -> dict:
        """Линия второй стороны: состав из `qmem` (если был) и `active`/`why` из её `st.quo`."""
        quo = self.reserve_peer_state().get("quo")
        out = dict(quo) if isinstance(quo, dict) else {}
        peer = self.rsv_get("rsv_quorum").get("peer") or {}
        if peer.get("members") and not out.get("members"):   # её st.quo — действующий состав; qmem — пока st нет
            out.update(l=peer.get("l"), g=int(peer.get("g") or 0), members=list(peer.get("members") or []))
        return out

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
        proposed = self.reserve_quorum_proposed()
        _nodes, big = self._quorum_gw_nodes(side)
        keys_ok = self.reserve_quorum_keys_ok(m) if hasattr(self, "reserve_quorum_keys_ok") else True
        if hasattr(self, "reserve_quorum_keys_confirmed"):
            # смена поколения (порог, новое M, возврат по точкам, «🔄 Сменить код») линию не выключает:
            # до подтверждения новых ключей у всех действует прежнее — ответчик отвечает, счёт идёт (§3.7.10);
            # первое включение ждёт подтверждения
            keys_ok = self.reserve_quorum_keys_confirmed(keys_ok)
        return qm.off_reasons(m if m is not None else proposed, acked=m is not None,
                              timing_problem=self.reserve_quorum_timing().check(), overlaps=self._quorum_overlaps(),
                              big_slots=big, keys_ok=keys_ok)

    def reserve_quorum_active(self) -> bool:
        return not self.reserve_quorum_off_reasons()

    def reserve_quorum_act_leftover(self) -> None:
        """Ключ `app.reserve.quorum.act` снят: оставшийся в app.yaml не читается — строка журнала один раз."""
        if self.__dict__.get("_quo_act_told"):
            return
        self.__dict__["_quo_act_told"] = True
        if settings.get("app.reserve.quorum.act", None) is not None:
            log.warning("кворум: reserve.quorum.act в app.yaml больше не читается — решения по кворуму всегда")

    def reserve_quorum_decisions(self) -> bool:
        """Голосов хватает для решений (v ≥ 2: есть шлюз в составе); при v = 1 линия живёт
        только зондом, счётом и окном кода (Р4)."""
        m = self.reserve_quorum_mine()
        return bool(self.reserve_quorum_active() and m is not None and m.enabled)

    def reserve_quorum_wanted(self) -> bool:
        """Условия включения без ключей: пара с `quo1`, подтверждённый состав с шлюзом,
        цифры и блок в порядке — ключи переводятся под кворум только после `qmem_ok`."""
        why = [c for c in self.reserve_quorum_off_reasons() if c != "keys"]
        return not why

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
        m = self.reserve_quorum_mine() or self.reserve_quorum_proposed()
        if m is None:
            return {}
        why = self.reserve_quorum_off_reasons()
        from awgbot.infra import quoprobe
        return {"l": m.l, "g": int(m.g), "members": list(m.members), "active": not why, "why": why[:8],
                "port": int(quoprobe.port())}                 # порт моего ответчика — зонду второй стороны

    def reserve_quorum_epoch(self) -> int:
        """Эпоха своей линии (журнал); без журнала — 0."""
        try:
            return int(self.db.jrn_meta()["epoch"])
        except Exception:                                     # noqa: BLE001
            return 0
