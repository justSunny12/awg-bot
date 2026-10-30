"""
vote.py — часть пакета reserve (см. __init__): переезд по взглядам на стороне
резерва — матрица случаев (README §6i), набор голосующих, `need` →
`share` → процесс открытия по двум долям, строка Н4 (окно и приём
аварийного кода — codewin), `view` основе и шлюзам, `state` шлюзам. Общие
предусловия любого открытия (кнопка, доли, код) — `reserve_open_block`;
переезд по взглядам сверх них ждёт `min_interval_minutes` после
состоявшегося автоматического переезда и FAIL_DAMPER_S после срыва открытия.

Голосующий — шлюз основы с агентом `rsv1`, долей текущего поколения и живым
гостевым каналом (сессия открыта, линк не «down» по счётчику и пробе).
Недоступный шлюз в набор не входит и не ждётся: решение — синхронный
пересчёт по текущему набору при каждом такте и каждом слове; единственный
таймер — демпфер случаев 3 и Н3 (rfblock.py), сбрасывает его только «вижу».

Чистое правило — `decide(reserve_sees, views)`; состояние — в памяти процесса
(живость, взгляды) и в `rsv_view` (случай, набор, need, окно кода).

Имён шлюзов основы у резерва нет: `voters` в `view` и `ask` — только номера
слотов, в текстах резерва шлюз — «шлюз N», имена подставляет основа сама.
"""
from __future__ import annotations

import json
import time

from awgbot.core import config, settings
from awgbot.util import livestep, rsvkeys, timeutil

from ..types import Notification
from ..gateway_link.common import log
from .state import _LOCK

NEED_RETRY_S = 30                 # доля не пришла — need следующему «не вижу»
CODE_TRIES = 3                    # попыток кода в час
FAIL_DAMPER_S = 120               # после срыва открытия — пауза перед новой попыткой по взглядам
CODE_WINDOW_S = 3600


def decide(reserve_sees: bool, views: dict) -> str:
    """Случай матрицы (README §6i) по взглядам голосующих: views — слот → "see" | "unseen".
    Возвращает "1", "2", "3", "4", "5", "6", "H3", "H4"."""
    seen = [s for s, v in views.items() if v == "see"]
    unseen = [s for s, v in views.items() if v == "unseen"]
    if not reserve_sees:
        if unseen:
            return "1" if not seen else "5"
        return "2" if seen else "H4"
    if not seen:
        return "3" if unseen else "H3"
    return "4" if unseen else "6"


MOVE_CASES = ("1", "5")
RF_CASES = ("3", "H3")


class ReserveVoteMixin:
    # ── память процесса ─────────────────────────────────────────────────────
    @property
    def _gwv(self) -> dict:
        """слот → {"online", "seen" (с какого момента не видит; пусто — видит),
        "m_gen", "lv": Liveness, "down_since", "since"}."""
        return self.__dict__.setdefault("_rsv_gwviews", {})

    def _gw_thresholds(self) -> tuple[int, int]:
        return (settings.get_int("app.reserve.suspect_seconds", 40), settings.get_int("app.reserve.probe_seconds", 15))

    @staticmethod
    def reserve_gw_name(slot: int) -> str:
        """Шлюз основы в текстах резерва — по номеру слота: имён второй стороне нет."""
        return f"шлюз {int(slot)}"

    # ── сессии гостевого канала ─────────────────────────────────────────────
    def reserve_guest_opened(self, slot: int, hello: dict) -> list[tuple]:
        """`hello` шлюза: в набор; ему — `view` и `state` сразу."""
        rec = self._gwv.setdefault(int(slot), {"lv": livestep.Liveness()})
        rec.update(online=True, seen=str(hello.get("seen") or ""), m_gen=int(hello.get("m_gen") or 0),
                   caps=list(hello.get("caps") or []), since=timeutil.now_iso(), down_since="")
        livestep.word(rec["lv"], time.monotonic())
        log.info("резерв: шлюз %s основы на гостевом канале (взгляд: %s, доля поколения %s)", slot,
                 "не видит" if rec["seen"] else "видит", rec["m_gen"])
        actions = [("gw", int(slot), "view", self._view_body()), ("gw", int(slot), "state", self._state_body())]
        return actions + self.reserve_vote_eval()

    def reserve_guest_closed(self, slot: int) -> list[tuple]:
        rec = self._gwv.get(int(slot))
        if rec:
            rec["online"] = False
            rec["closed_mono"] = time.monotonic()            # обрыв сессии — подозрение, не «недоступен»
        return self.reserve_vote_eval()

    def reserve_guest_in(self, slot: int, kind: str, body: dict) -> list[tuple]:
        """Слово шлюза по гостевому каналу."""
        rec = self._gwv.setdefault(int(slot), {"lv": livestep.Liveness(), "online": True})
        livestep.word(rec["lv"], time.monotonic())
        if kind == "ping":
            return []                                         # pong отдаёт слушатель канала сам
        if kind == "pong":
            livestep.pong(rec["lv"], time.monotonic(), str(body.get("n") or ""))
            return []
        if kind == "seen":
            rec["seen"] = str(body.get("seen") or "")
            log.info("резерв: шлюз %s основы — %s", slot, "не видит её с " + rec["seen"] if rec["seen"] else "видит её")
            return self.reserve_vote_eval()
        if kind == "share":
            return self._share_in(int(slot), str(body.get("share") or ""), int(body.get("m_gen") or 0))
        if kind in ("qview", "qshare") and hasattr(self, "reserve_quorum_guest_in_kind"):
            return self.reserve_quorum_guest_in_kind(int(slot), kind, body)
        return []

    # ── такт ────────────────────────────────────────────────────────────────
    def reserve_vote_tick(self) -> list[tuple]:
        """Такт 5 с: живость шлюзов по счётчикам сторон зоны, проба `ping`,
        пересчёт матрицы, демпфер и просьбы (rfblock)."""
        if not self.reserve_paired():
            self.__dict__.pop("_rsv_gwviews", None)
            return []
        suspect_s, probe_s = self._gw_thresholds()
        now = time.monotonic()
        actions: list[tuple] = []
        for slot, rec in list(self._gwv.items()):
            if not rec.get("online"):
                continue
            act = livestep.step(rec["lv"], now, self.reserve_zone_link_rx(slot), suspect_s=suspect_s, probe_s=probe_s)
            if act == "ping":
                actions.append(("gw", slot, "ping", {"n": rec["lv"].nonce}))
            elif act == "down":
                rec["down_since"] = timeutil.now_iso()
                log.warning("резерв: шлюз %s основы не отвечает по линку", slot)
            elif act == "up":
                rec["down_since"] = ""
        actions += self.reserve_vote_eval()
        actions += self.reserve_rf_tick()
        return actions

    # ── набор и случай ──────────────────────────────────────────────────────
    def _voters(self) -> dict:
        """слот → "see" | "unseen" — только голосующие."""
        m_gen = int(self.rsv_get("rsv_pkg_in").get("m_gen") or 0)
        out = {}
        for slot, rec in self._gwv.items():
            if rec["lv"].state == "down":
                continue
            if not rec.get("online"):
                # сессия оборвалась: шлюз в наборе ещё окно живости (переподключится с отступом)
                suspect_s, probe_s = self._gw_thresholds()
                if livestep.mono_due(rec.get("closed_mono"), time.monotonic(), suspect_s + probe_s):
                    continue
            if not m_gen or int(rec.get("m_gen") or 0) != m_gen:
                continue
            out[int(slot)] = "unseen" if rec.get("seen") else "see"
        return out

    def _quorum_rules(self) -> bool:
        """Кворум второй стороны включён и решает: матрица — только Н3 (§3.7.9)."""
        if not hasattr(self, "reserve_quorum_peer_line") or not hasattr(self, "reserve_quorum_act"):
            return False
        return bool(self.reserve_quorum_peer_line().get("active")) and self.reserve_quorum_act()

    def _liveness_views(self) -> dict:
        """Под кворумом доли прямой у шлюзов нет — набор для Н3 по живости гостевого
        канала, взгляд — `qview` (uns → unseen)."""
        l = str(self.reserve_quorum_peer_line().get("l") or "")
        gv = self.reserve_quorum_gw_views(l) if l and hasattr(self, "reserve_quorum_gw_views") else {}
        out = {}
        for slot, rec in self._gwv.items():
            if rec["lv"].state == "down" or not rec.get("online"):
                continue
            w = str((gv.get(f"{l}{int(slot)}") or {}).get("w") or "")
            out[int(slot)] = "unseen" if w == "uns" else "see"
        return out

    def _absent_slots(self) -> list[int]:
        """Слоты основы с rsv1 (по `st.voters`), которых нет в наборе."""
        known = [int(v.get("slot") or 0) for v in self.reserve_peer_state().get("voters") or [] if isinstance(v, dict)]
        return [s for s in known if s and s not in self._voters()]

    def reserve_gw_views(self) -> list[dict]:
        """Взгляды шлюзов основы для экранов: имя, в наборе ли, видит ли, с какого момента."""
        voters = self._voters()
        out = []
        for v in self.reserve_peer_state().get("voters") or []:
            if not isinstance(v, dict):
                continue
            slot = int(v.get("slot") or 0)
            rec = self._gwv.get(slot) or {}
            out.append({"slot": slot, "name": self.reserve_gw_name(slot), "voter": slot in voters,
                        "seen": str(rec.get("seen") or ""), "down_since": str(rec.get("down_since") or "")})
        return out

    def _view_body(self) -> dict:
        """`view` шлюзам: взгляд резерва, набор, моё направление, поколение пакета."""
        return {"seen": self.reserve_down_since() if self.reserve_peer_seen() == "down" else "",
                "voters": [{"slot": s, "see": v == "see"} for s, v in self._voters().items()][:16],
                "in": self.reserve_in(), "pkg_gen": int(self.rsv_get("rsv_pkg_in").get("gen") or 0)}

    def _state_body(self, reason: str = "") -> dict:
        inn = self.rsv_get("rsv_in")
        serving = str(inn.get("state") or "idle") in ("taking", "serving", "releasing")   # до возврата — открыто
        body = {"state": "serving" if serving else "idle", "move": int(inn.get("gen") or 0)}
        if reason:
            body["reason"] = str(reason)[:200]                 # срыв открытия после отданной доли — шлюзу Т-РА5
        return body

    def reserve_gw_state_actions(self, force: bool = False, reason: str = "") -> list[tuple]:
        """`state{serving|idle, move}` всем шлюзам основы по смене (или force)."""
        body = self._state_body(reason)
        packed = json.dumps(body, sort_keys=True)
        if not force and self.__dict__.get("_rsv_state_sent") == packed:
            return []
        self.__dict__["_rsv_state_sent"] = packed
        return [("gw", s, "state", body) for s, rec in self._gwv.items() if rec.get("online")]

    def reserve_vote_eval(self) -> list[tuple]:
        """Пересчёт случая по текущему набору; переходы — действия."""
        if not self.reserve_paired():
            return []
        quorum = self._quorum_rules()
        views = self._liveness_views() if quorum else self._voters()
        sees = self.reserve_peer_seen() != "down"
        case = decide(sees, views)
        if quorum and case != "H3":
            case = "6"                                        # переезд и код — у кандидата кворума; матрице остаётся Н3
        view = self.rsv_get("rsv_view")
        prev = str(view.get("case") or "")
        actions: list[tuple] = []
        with _LOCK:
            if case != prev:
                since = self.reserve_down_since() if not sees else (
                    min([r.get("seen") for s, r in self._gwv.items() if s in views and r.get("seen")] or [timeutil.now_iso()]))
                self.rsv_update("rsv_view", case=case, case_since=since, case_at=timeutil.now_iso(),
                                voters={str(s): v for s, v in views.items()}, absent=self._absent_slots())
                log.info("резерв: случай матрицы %s → %s (набор: %s)", prev or "—", case,
                         ", ".join(f"{s}:{v}" for s, v in views.items()) or "пусто")
            else:
                self.rsv_update("rsv_view", voters={str(s): v for s, v in views.items()}, absent=self._absent_slots())
        actions += self._view_to_gateways()
        actions += self._view_to_primary(case)
        if not quorum:
            actions += self._code_window(case)                # под кворумом окно кода ведёт кандидат
        if case in MOVE_CASES:
            actions += self._auto_move(case, views)
        elif case != "3" and self.rsv_get("rsv_view").get("need"):
            self.rsv_update("rsv_view", need=None)            # в случае 3 просьбу ставит rfblock (без ответа основы)
        actions += self.reserve_gw_state_actions()
        return actions

    def _view_to_gateways(self) -> list[tuple]:
        body = self._view_body()
        packed = json.dumps(body, sort_keys=True)
        if self.__dict__.get("_rsv_gwview_sent") == packed:
            return []
        self.__dict__["_rsv_gwview_sent"] = packed
        return [("gw", s, "view", body) for s, rec in self._gwv.items() if rec.get("online")]

    def _view_to_primary(self, case: str) -> list[tuple]:
        """`view` основе по каналу пары — при смене случая (3, 4, Н3 — её уведомления)."""
        if case not in ("3", "4", "H3") or not self.reserve_online():
            return []
        if case == "H3" and (not self._nogw_worth() or not self._absent_slots()):
            return []                                         # резерву нечем дать РФ-доступ или у основы нет шлюзов
        view = self.rsv_get("rsv_view")
        body = {"case": case, "since": str(view.get("case_since") or ""),
                "voters": [{"slot": s, "see": v == "see"} for s, v in self._voters().items()][:16]}   # имена основа знает сама
        packed = json.dumps(body, sort_keys=True)
        if view.get("view_sent") == packed:
            return []
        self.rsv_update("rsv_view", view_sent=packed)
        return [("send", "view", body)]

    # ── автоматический переезд (случаи 1 и 5) ───────────────────────────────
    def reserve_open_block(self, *, need_share: bool = True) -> str:
        """Общие предусловия открытия зоны (кнопка, доли, код): зона свободна,
        мои пользователи на этом сервере, готовность включена, направление «к себе» готово,
        пакет (и доля) на месте. Пусто — можно; иначе причина."""
        if not self.reserve_zone_free():
            return "зона занята"
        if self.reserve_out() != "home":
            return "мои пользователи не на этом сервере"
        if not settings.get_bool("app.reserve.ready", False):
            return "готовность выключена"
        ok, why = self.reserve_directions()["in"]
        if not ok:
            return why
        pkg_in = self.rsv_get("rsv_pkg_in")
        if not pkg_in.get("gen") or (need_share and not pkg_in.get("share")):
            return "данных для переезда нет"
        if need_share and pkg_in.get("has_dns") is False:
            # запись меняет процесс открытия на резерве — без конверта DNS (провайдер «вручную») ему нечем;
            # карточка ставит строку под «автоматический аварийный переезд невозможен: …», уведомление — хвост
            return f"DNS-запись у «{self.reserve_peer_name()}» настраивается вручную"
        return ""

    def reserve_auto_block(self) -> str:
        """Пусто — переезд по взглядам возможен; иначе причина (строка карточки)."""
        why = self.reserve_open_block()
        if why:
            return why
        inn = self.rsv_get("rsv_in")
        fail_at = str(inn.get("fail_at") or "")
        if fail_at and str(inn.get("state") or "idle") == "idle":
            try:
                if timeutil.elapsed(timeutil.parse_iso(fail_at)) < FAIL_DAMPER_S:
                    return "после отказа открытия — пауза две минуты"
            except ValueError:
                pass
        last = str(inn.get("since") or "")
        if last and str(inn.get("state") or "idle") == "idle" and inn.get("ended_auto"):
            try:
                mins = settings.get_int("app.reserve.min_interval_minutes", 30)
                if timeutil.elapsed(timeutil.parse_iso(last)) < mins * 60:
                    return f"между автоматическими переездами не меньше {mins} мин"
            except ValueError:
                pass
        return ""

    def _auto_move(self, case: str, views: dict) -> list[tuple]:
        why = self.reserve_auto_block()
        view = self.rsv_get("rsv_view")
        if why:
            if view.get("auto_block") != why:
                self.rsv_update("rsv_view", auto_block=why)
                log.warning("резерв: переезд по взглядам (случай %s) не идёт: %s", case, why)
            return []
        if view.get("auto_block"):
            self.rsv_update("rsv_view", auto_block="")
        return self._need_send(case, views, kind="dead")

    def _need_send(self, case: str, views: dict, kind: str) -> list[tuple]:
        """`need` первому шлюзу «не вижу», которого ещё не просили (или просили
        давно без ответа)."""
        need = dict(self.rsv_get("rsv_view").get("need") or {})
        unseen = [s for s, v in views.items() if v == "unseen"]
        if not unseen:
            return []
        if self.reserve_in() != "idle" or self.reserve_out() != "home":
            return []                                         # зона занята или мои пользователи не здесь — долю не просим
        if need.get("slot") in unseen and not livestep.mono_due(need.get("mono"), time.monotonic(), NEED_RETRY_S):
            return []
        asked = set(int(x) for x in need.get("asked") or [])
        pick = next((s for s in unseen if s not in asked), None)
        if pick is None:
            asked, pick = set(), unseen[0]
        asked.add(pick)
        since = str(self.rsv_get("rsv_view").get("case_since") or "")
        self.rsv_update("rsv_view", need={"slot": pick, "at": timeutil.now_iso(), "mono": time.monotonic(),
                                          "case": case, "kind": kind, "asked": sorted(asked)})
        log.warning("резерв: прошу долю у шлюза %s основы (случай %s)", pick, case)
        return [("gw", pick, "need", {"case": case, "since": since,
                                      "m_gen": int(self.rsv_get("rsv_pkg_in").get("m_gen") or 0)})]

    def _share_in(self, slot: int, share: str, m_gen: int) -> list[tuple]:
        need = dict(self.rsv_get("rsv_view").get("need") or {})
        if int(need.get("slot") or 0) != int(slot):
            log.info("резерв: доля от шлюза %s без просьбы — не беру", slot)
            return []
        rec = self._gwv.get(int(slot)) or {}
        if not rec.get("seen"):
            log.info("резерв: шлюз %s уже видит основу — доля не нужна", slot)
            self.rsv_update("rsv_view", need=None)
            return []
        pkg_in = self.rsv_get("rsv_pkg_in")
        if int(pkg_in.get("m_gen") or 0) != int(m_gen):
            log.warning("резерв: доля шлюза %s поколения %s, а пакет — %s", slot, m_gen, pkg_in.get("m_gen"))
            return []
        points = [[rsvkeys.RESERVE_X, str(pkg_in.get("share") or "")], [rsvkeys.gateway_x(int(slot)), share]]
        kind = str(need.get("kind") or "dead")
        self.rsv_update("rsv_view", need=None, moved_by={"slot": int(slot), "case": need.get("case"), "at": timeutil.now_iso()})
        return self.reserve_open_by_points({"shares": points}, kind, str(need.get("case") or "1"))

    def reserve_open_by_points(self, secret: dict, kind: str, case: str) -> list[tuple]:
        """Процесс открытия по долям или по коду: как `go` основы, но без K;
        вид — dead | rf | code; итог админу — после захвата Telegram гостем."""
        why = self.reserve_auto_block() if kind != "code" else self._code_block()
        if why:
            log.warning("резерв: процесс открытия (%s) не запущен: %s", kind, why)
            return [("notify", [Notification(tg_id=int(config.ADMIN_ID),
                                             text=_fail_text(self.reserve_peer_name(), f"{why} — аварийный переезд невозможен"))])]
        pkg_in = self.rsv_get("rsv_pkg_in")
        self.reserve_zone_shell_ensure()
        payload = self._open_payload(kind)
        payload.update(secret)
        extra = self._hosting_extra(case)
        with _LOCK:
            if not self.reserve_zone_free():                  # проверка и запись — под одним замком
                return []
            self._relay_yield()
            from awgbot.infra import rsvzone
            rsvzone.open_status_write("launching")
            self.reserve_set_in("taking", kind=kind, gen=int(pkg_in.get("gen") or 0), why=case, extra=extra,
                                by="code" if "code" in secret else "share",
                                open_started=timeutil.now_iso(), deadline=time.monotonic() + self._open_wait_s(),
                                up_sent=False, tg_taken=False, ended_auto=True,
                                dead_since=str(self.rsv_get("rsv_view").get("case_since") or ""))
            self._in_step("open")
        try:
            self._rsvopen_launch(payload)
        except Exception as e:                                # noqa: BLE001
            self.reserve_set_in("idle", step="", reason=str(e))
            return [("notify", [Notification(tg_id=int(config.ADMIN_ID), text=_fail_text(self.reserve_peer_name(), str(e)))])]
        log.warning("резерв: процесс открытия по %s запущен (случай %s, пакет %s)", "коду" if kind == "code" else "долям",
                    case, pkg_in.get("gen"))
        return [("fw",)] + self.reserve_gw_state_actions()

    def _open_wait_s(self) -> int:
        from .inbound import OPEN_WAIT_S
        return OPEN_WAIT_S

    def _hosting_extra(self, case: str) -> str:
        """Хвост Т-РУ4: частично (случай 5) и «не на связи с этим сервером»."""
        parts = []
        views = self._voters()
        if case == "5":
            unseen = [self.reserve_gw_name(s) for s, v in views.items() if v == "unseen"]
            seen = [self.reserve_gw_name(s) for s, v in views.items() if v == "see"]
            parts.append("его шлюзы — частично: не видят " + ", ".join(unseen) + ", видят " + ", ".join(seen))
        absent = [self.reserve_gw_name(s) for s in self._absent_slots()]
        if absent:
            parts.append(", ".join(absent) + " не на связи с этим сервером")
        return "; ".join(parts)


def _fail_text(primary: str, why: str) -> str:
    from awgbot.domain.notices import reserve as rn
    return rn.hosting_failed(primary, why)
