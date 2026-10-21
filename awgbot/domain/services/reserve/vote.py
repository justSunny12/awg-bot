"""
vote.py — часть пакета reserve (см. __init__): переезд по взглядам на стороне
резерва — матрица случаев (README §6i), набор голосующих, `need` →
`share` → процесс открытия по двум долям, строка Н4 (сразу Т-РУ7, Т-РУ7б и
приём аварийного кода), `view` основе и шлюзам, `state` шлюзам.

Голосующий — шлюз основы с агентом `rsv1`, долей текущего поколения и живым
гостевым каналом (сессия открыта, линк не «down» по счётчику и пробе).
Недоступный шлюз в набор не входит и не ждётся: решение — синхронный
пересчёт по текущему набору при каждом такте и каждом слове; единственный
таймер — демпфер случаев 3 и Н3 (rfblock.py), сбрасывает его только «вижу».

Чистое правило — `decide(reserve_sees, views)`; состояние — в памяти процесса
(живость, взгляды) и в `rsv_view` (случай, набор, need, окно кода).
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

    def reserve_gw_name(self, slot: int) -> str:
        for v in self.reserve_peer_state().get("voters") or []:
            if isinstance(v, dict) and int(v.get("slot") or 0) == int(slot):
                return str(v.get("name") or f"Шлюз {slot}")
        return f"Шлюз {slot}"

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
            out.append({"slot": slot, "name": str(v.get("name") or f"Шлюз {slot}"), "voter": slot in voters,
                        "seen": str(rec.get("seen") or ""), "down_since": str(rec.get("down_since") or "")})
        return out

    def _view_body(self) -> dict:
        """`view` шлюзам: взгляд резерва, набор, моё направление, поколение пакета."""
        return {"seen": self.reserve_down_since() if self.reserve_peer_seen() == "down" else "",
                "voters": [{"slot": s, "see": v == "see"} for s, v in self._voters().items()][:16],
                "in": self.reserve_in(), "pkg_gen": int(self.rsv_get("rsv_pkg_in").get("gen") or 0)}

    def _state_body(self) -> dict:
        inn = self.rsv_get("rsv_in")
        serving = str(inn.get("state") or "idle") in ("taking", "serving", "releasing")   # до возврата — открыто
        return {"state": "serving" if serving else "idle", "move": int(inn.get("gen") or 0)}

    def reserve_gw_state_actions(self, force: bool = False) -> list[tuple]:
        """`state{serving|idle, move}` всем шлюзам основы по смене (или force)."""
        body = self._state_body()
        packed = json.dumps(body, sort_keys=True)
        if not force and self.__dict__.get("_rsv_state_sent") == packed:
            return []
        self.__dict__["_rsv_state_sent"] = packed
        return [("gw", s, "state", body) for s, rec in self._gwv.items() if rec.get("online")]

    def reserve_vote_eval(self) -> list[tuple]:
        """Пересчёт случая по текущему набору; переходы — действия."""
        if not self.reserve_paired():
            return []
        views = self._voters()
        sees = self.reserve_peer_seen() != "down"
        case = decide(sees, views)
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
        actions += self._code_window(case)
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
                "voters": [{"slot": s, "name": self.reserve_gw_name(s), "see": v == "see"} for s, v in self._voters().items()][:16]}
        packed = json.dumps(body, sort_keys=True)
        if view.get("view_sent") == packed:
            return []
        self.rsv_update("rsv_view", view_sent=packed)
        return [("send", "view", body)]

    # ── автоматический переезд (случаи 1 и 5) ───────────────────────────────
    def reserve_auto_block(self) -> str:
        """Пусто — переезд по взглядам возможен; иначе причина (строка карточки)."""
        if self.reserve_in() != "idle":
            return "зона занята"
        if self.reserve_out() != "home":
            return "мои клиенты не на этом сервере"
        if not settings.get_bool("app.reserve.ready", False):
            return "готовность выключена"
        ok, why = self.reserve_directions()["in"]
        if not ok:
            return why
        pkg_in = self.rsv_get("rsv_pkg_in")
        if not pkg_in.get("gen") or not pkg_in.get("share"):
            return "данных для переезда нет"
        inn = self.rsv_get("rsv_in")
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
            return []                                         # зона занята или мои клиенты не здесь — долю не просим
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
            return [("notify", [Notification(tg_id=int(config.ADMIN_ID), text=_fail_text(self.reserve_peer_name(), why))])]
        pkg_in = self.rsv_get("rsv_pkg_in")
        self.reserve_zone_shell_ensure()
        payload = self._open_payload(kind)
        payload.update(secret)
        extra = self._hosting_extra(case)
        with _LOCK:
            if self.reserve_in() != "idle":                   # проверка и запись — под одним замком
                return []
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
            parts.append("его шлюзы — частично: не видят " + ", ".join(f"«{n}»" for n in unseen)
                         + ", видят " + ", ".join(f"«{n}»" for n in seen))
        absent = [self.reserve_gw_name(s) for s in self._absent_slots()]
        if absent:
            parts.append(", ".join(f"«{n}»" for n in absent) + (" не на связи с этим сервером" if len(absent) == 1 else " не на связи с этим сервером"))
        return "; ".join(parts)

    # ── Н4: окно аварийного кода ────────────────────────────────────────────
    def reserve_code_window(self) -> dict:
        """{"open": bool, "since": iso} — приём кода открыт только в Н4."""
        cw = self.rsv_get("rsv_view").get("code") or {}
        return {"open": bool(cw.get("open")), "since": str(cw.get("since") or "")}

    def _code_window(self, case: str) -> list[tuple]:
        view = self.rsv_get("rsv_view")
        cw = dict(view.get("code") or {})
        actions: list[tuple] = []
        primary = self.reserve_peer_name()
        if case == "H4" and self.reserve_in() == "idle":
            if not cw.get("open"):
                since = str(view.get("case_since") or timeutil.now_iso())
                if cw.get("episode") == since:
                    # тот же эпизод (окно закрывалось и открылось снова) — без повторных Т-РУ7/7б
                    cw.update(open=True)
                    self.rsv_update("rsv_view", code=cw)
                    return actions
                opened = bool(self.rsv_get("rsv_peer_admin").get("opened"))
                pid = self.reserve_peer_admin_id()
                notes = [Notification(tg_id=int(config.ADMIN_ID), text=self._rn().code_needed_reserve(primary, since, opened))]
                if pid and opened:
                    notes.append(Notification(tg_id=pid, force_sound=True,
                                              text=self._rn().code_needed_primary(primary, str(config.SERVER_NAME or ""), since),
                                              keyboard="peer_code"))
                self.rsv_update("rsv_view", code={"open": True, "since": since, "told": bool(pid and opened), "tries": [],
                                                  "episode": since})
                log.warning("резерв: Н4 — основа и её шлюзы не отвечают; приём аварийного кода открыт")
                actions.append(("notify", notes))
        elif cw.get("open"):
            why = "primary_seen" if self.reserve_peer_seen() != "down" else ("gw_unseen" if case in MOVE_CASES else "gw_sees")
            self.rsv_update("rsv_view", code={"open": False, "closed_why": why, "closed_at": timeutil.now_iso(),
                                              "episode": str(cw.get("episode") or ""), "told": bool(cw.get("told"))})
            log.info("резерв: приём аварийного кода закрыт (%s)", why)
            pid = self.reserve_peer_admin_id()
            if cw.get("told") and pid:
                gw_name = next((self.reserve_gw_name(s) for s in self._voters()), primary)
                actions.append(("notify", [Notification(tg_id=pid, text=self._rn().code_window_closed(
                    primary if why == "primary_seen" else gw_name, why))]))
        return actions

    def _code_block(self) -> str:
        if not self.reserve_code_window()["open"]:
            return "приём кода закрыт"
        if self.reserve_in() != "idle":
            return "зона занята"
        if self.reserve_out() != "home":
            return "мои клиенты не на этом сервере"
        pkg_in = self.rsv_get("rsv_pkg_in")
        if not pkg_in.get("gen") or not pkg_in.get("share"):
            return "данных для переезда нет"
        return ""

    def reserve_code_in(self, uid: int, text: str) -> dict:
        """Код из чата роли админа основы — транзитом в процесс открытия (M и K
        собирает только он). {"ok": True, "pending": True} — процесс запущен,
        итог придёт уведомлением; иначе {"ok": False, "why": closed | wrong |
        limit | busy, "left": n}. «wrong» здесь — код не разобран (контрольная
        сумма); не подошедший к пакету код процесс отдаст через `reserve_code_failed`."""
        if not self.reserve_code_sender_ok(int(uid)):
            log.warning("резерв: аварийный код не от админа основы (%s) — отказ", uid)
            return {"ok": False, "why": "closed"}
        if not self.reserve_code_window()["open"]:
            return {"ok": False, "why": "closed"}
        cw = dict(self.rsv_get("rsv_view").get("code") or {})
        tries = self._code_tries(cw)
        if len(tries) >= CODE_TRIES:
            return {"ok": False, "why": "limit", "left": 0}
        norm = rsvkeys.normalize(text) or ""
        try:
            rsvkeys.code_decode(norm)
        except ValueError:
            return self._code_try_failed(cw, "не разобран")
        pkg_in = self.rsv_get("rsv_pkg_in")
        actions = self.reserve_open_by_points({"share": [rsvkeys.RESERVE_X, str(pkg_in.get("share") or "")], "code": norm},
                                              "code", "H4")
        if self.reserve_in() != "taking":
            return {"ok": False, "why": "busy", "actions": actions}
        cw.update(open=False, closed_why="used", closed_at=timeutil.now_iso(), pending=True)
        self.rsv_update("rsv_view", code=cw, moved_by={"code": True, "at": timeutil.now_iso()})
        return {"ok": True, "pending": True, "actions": actions}

    @staticmethod
    def _code_tries(cw: dict) -> list:
        now = time.time()
        return [t for t in cw.get("tries") or [] if now - float(t) < CODE_WINDOW_S]

    def _code_try_failed(self, cw: dict, why: str) -> dict:
        tries = self._code_tries(cw) + [time.time()]
        cw["tries"] = tries
        self.rsv_update("rsv_view", code=cw)
        log.warning("резерв: аварийный код %s (попытка %d из %d)", why, len(tries), CODE_TRIES)
        return {"ok": False, "why": "wrong", "left": max(0, CODE_TRIES - len(tries))}

    def reserve_code_failed(self, reason: str) -> list[tuple]:
        """Процесс открытия по коду отказал: не подошёл (ключ пакета не снялся) —
        попытка в счёт, окно снова открыто, админу основы Т-РН3; иная причина —
        окно открыто, админу основы одной строкой."""
        cw = dict(self.rsv_get("rsv_view").get("code") or {})
        if not cw.get("pending"):
            return []
        pid = self.reserve_peer_admin_id()
        wrong = any(w in reason for w in ("ключ пакета", "доли не сошлись", "аварийный код"))
        cw.update(open=True, pending=False, closed_why="")
        if wrong:
            res = self._code_try_failed(cw, "не подошёл")
            cw = dict(self.rsv_get("rsv_view").get("code") or {})
            cw.update(open=True, pending=False)
            self.rsv_update("rsv_view", code=cw)
            text = self._rn().code_wrong(int(res.get("left") or 0))
        else:
            self.rsv_update("rsv_view", code=cw)
            text = self._rn().code_open_failed(self.reserve_peer_name(), reason)
        return [("notify", [Notification(tg_id=pid, text=text)])] if pid else []

    def _rn(self):
        from awgbot.domain.notices import reserve as rn
        return rn


def _fail_text(primary: str, why: str) -> str:
    from awgbot.domain.notices import reserve as rn
    return rn.hosting_failed(primary, why)
