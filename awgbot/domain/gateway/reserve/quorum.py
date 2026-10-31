"""
quorum.py — часть пакета reserve агента (см. __init__): кворум резерва на
шлюзе — раздел `quorum` документа конфигурации (файл
`/var/lib/awg-gw/quorum.json`, 0600: линия, поколение, состав, порог, конфиги
зондов к основе и к клону, точка, эпоха), интерфейсы зондов в пространстве
`awg-quo` (скрипт install/quorum-probe-setup.sh через infra/quoprobe), пробы
и взгляд голосующего (правило `util/quorum/voter`), `qview` резерву по
гостевому каналу и своему серверу по каналу конфигурации, строка панели
(Т-К17) и поле `quo` снимка.

Зонд к основе (`awgq<l>h`) стоит всегда, пока есть раздел; зонд к клону
(`awgq<l>r`) — пока пользователи основы у резерва (слово `away` основы или
`serving` резерва в reserve.json). Свои отметки хода (`_KEEP_KEYS`) новый
документ не стирает.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from awgbot.util import fsatomic, livestep, timeutil
from awgbot.util.quorum import timing as qt
from awgbot.util.quorum.probe import Probe, Reply
from awgbot.util.quorum.timing import Timing
from awgbot.util.quorum.voter import Voter

from ..base import log
from .store import STORE_LOCK                                 # один замок на reserve.json и quorum.json

STORE_PATH = Path("/var/lib/awg-gw/quorum.json")
_KEEP_KEYS = ("views", "pledges", "told", "apply_mono", "share_given_at", "e_known")
APPLY_EVERY_S = 300


def _read(path: Path = STORE_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict, path: Path = STORE_PATH) -> None:
    fsatomic.write_private(path, json.dumps(data, ensure_ascii=False))


class GatewayQuorumMixin:
    _QUO_STORE = STORE_PATH

    # ── раздел документа ────────────────────────────────────────────────────
    def quorum_section(self) -> dict:
        return _read(self._QUO_STORE)

    def quorum_present(self) -> bool:
        return bool((self.quorum_section().get("probes") or {}).get("h"))

    def quorum_print(self) -> str:
        from awgbot.domain import gwcfg
        sec = self.quorum_section()
        return gwcfg.quorum_print(sec) if sec.get("l") else ""

    def quorum_apply(self, checked: dict | None) -> dict:
        """Раздел из документа (проверенный) или None — у сервера раздела нет."""
        if checked is None:
            if not self.quorum_section():
                return {"changed": False, "note": ""}
            self.quorum_remove()
            return {"changed": True, "note": "🗳 Голосование о сервере AWG снято — зонды опущены"}
        new = {"l": str(checked.get("l") or ""), "g": int(checked.get("g") or 0),
               "members": list(checked.get("members") or []), "t": int(checked.get("t") or 0),
               "port": int(checked.get("port") or 0), "probes": dict(checked.get("probes") or {}),
               "point": dict(checked.get("point") or {}), "e": int(checked.get("e") or 0),
               "timing": dict(checked.get("timing") or {}), "applied_at": timeutil.now_iso()}
        with STORE_LOCK:
            cur = self.quorum_section()
            for k in _KEEP_KEYS:
                if k in cur:
                    new[k] = cur[k]
            changed = any(cur.get(k) != new[k] for k in ("l", "g", "members", "t", "port", "probes", "point"))
            _write(new, self._QUO_STORE)
        if int(cur.get("e") or 0) < new["e"]:
            self._quorum_epoch_in(new["e"])
        note = ""
        if not cur.get("l"):
            note = "🗳 Голосование о сервере AWG включено — зонд к нему поднят"
        self.quorum_probes_apply(force=True)
        return {"changed": changed, "note": note}

    def quorum_remove(self) -> None:
        from awgbot.infra import quoprobe
        try:
            quoprobe.wipe()
        except quoprobe.QuoError as e:
            log.warning("quorum: зонды не сняты: %s", e)
        try:
            os.unlink(str(self._QUO_STORE))
        except OSError:
            pass
        self.__dict__.pop("_gw_qvoters", None)

    # ── интерфейсы зондов ───────────────────────────────────────────────────
    def quorum_probe_ifaces(self) -> dict:
        """Имя → {conf, l, target, node_addr, resp, port}: `h` всегда, `r` — пока
        пользователи основы у резерва."""
        sec = self.quorum_section()
        l = str(sec.get("l") or "")
        probes = sec.get("probes") or {}
        if not l or not probes.get("h"):
            return {}
        out = {f"awgq{l}h": {"conf": str(probes["h"]), "l": l, "target": "h", **self._conf_addrs(str(probes["h"]), sec)}}
        if probes.get("r") and self._primary_away():
            out[f"awgq{l}r"] = {"conf": str(probes["r"]), "l": l, "target": "r", **self._conf_addrs(str(probes["r"]), sec)}
        return out

    def _primary_away(self) -> bool:
        rs = self.reserve_section() if hasattr(self, "reserve_section") else {}
        return str(rs.get("state") or "") == "away" or str(rs.get("reserve_state") or "") == "serving"

    @staticmethod
    def _conf_addrs(conf: str, sec: dict) -> dict:
        import re
        addr = re.search(r"(?m)^\s*Address\s*=\s*([0-9.]+)", conf)
        resp = re.search(r"(?m)^\s*AllowedIPs\s*=\s*([0-9.]+)", conf)
        from awgbot.infra import quoprobe
        return {"node_addr": addr.group(1) if addr else "", "resp": resp.group(1) if resp else "",
                "port": int(sec.get("port") or quoprobe.port())}

    def quorum_probes_apply(self, force: bool = False) -> dict:
        from awgbot.infra import quoprobe
        want = self.quorum_probe_ifaces()
        digest = json.dumps(sorted((k, v["conf"]) for k, v in want.items()))
        now = time.monotonic()
        if not force and self.__dict__.get("_quo_apply_digest") == digest and \
                not livestep.mono_due(self.__dict__.get("_quo_apply_mono"), now, APPLY_EVERY_S):
            return want
        self.__dict__["_quo_apply_digest"], self.__dict__["_quo_apply_mono"] = digest, now
        for name in sorted(set(quoprobe.standing()) - set(want)):
            try:
                quoprobe.down(name)
                log.info("quorum: зонд %s снят", name)
            except quoprobe.QuoError as e:
                log.warning("quorum: зонд %s не снят: %s", name, e)
        for name, rec in want.items():
            try:
                out = quoprobe.up(name, rec["conf"], int(rec.get("port") or 0))
                if "same" not in out:
                    log.info("quorum: зонд %s поднят", name)
            except quoprobe.QuoError as e:
                log.warning("quorum: зонд %s не поднялся: %s", name, e)
        return want

    # ── правило голосующего ─────────────────────────────────────────────────
    @property
    def _gw_qvoters(self) -> dict:
        return self.__dict__.setdefault("_gw_qvoters", {})

    def quorum_timing(self) -> Timing:
        """Цифры лидера из раздела `quorum` документа (те же ключи, что `app.reserve.quorum`);
        раздел без цифр — умолчания агента (`app.gateway.quorum`)."""
        from awgbot.core import settings
        q = self.quorum_section().get("timing")
        if not isinstance(q, dict) or not q:
            q = settings.get("app.gateway.quorum", {}) or {}
        q = q if isinstance(q, dict) else {}
        rf = q.get("rf_block_minutes", settings.get_int("app.gateway.rf_block_minutes", 15))
        return Timing.from_settings(q, rf)

    def quorum_my_node(self) -> str:
        sec = self.quorum_section()
        slot = int((self.reserve_section() if hasattr(self, "reserve_section") else {}).get("slot") or 0)
        return f"{sec.get('l')}{slot}" if sec.get("l") and slot else ""

    def quorum_own_view(self) -> tuple[str, str]:
        """Свой взгляд зонда на сервер AWG линии: (see | held | sus | uns | new | "", с какого момента);
        пусто — зонда нет (раздела quorum нет или шлюз не в составе)."""
        sec = self.quorum_section()
        l = str(sec.get("l") or "")
        if not l:
            return "", ""
        name = f"awgq{l}h"
        v = self._gw_qvoters.get(name)
        stored = dict((sec.get("views") or {}).get(name) or {})
        if v is not None:
            return str(v.view or ""), str(stored.get("since") or "")
        return str(stored.get("w") or ""), str(stored.get("since") or "")

    def _quorum_epoch_in(self, e: int) -> None:
        for v in self._gw_qvoters.values():
            v.epoch_in(int(e))
        self._quorum_epoch_persist(int(e))

    def _quorum_epoch_persist(self, e: int) -> None:
        """Известная эпоха — на диск (`e_known`): после перезапуска агента пробы идут с ней,
        а не с эпохой документа (после `qtake` прежний лидер иначе снова набирал бы «да» под
        старой эпохой до `qstate` резерва)."""
        with STORE_LOCK:
            sec = self.quorum_section()
            if sec.get("l") and int(sec.get("e_known") or 0) < int(e):
                sec["e_known"] = int(e)
                _write(sec, self._QUO_STORE)

    def quorum_runtime_plan(self) -> dict:
        return {"responder": None, "probes": self.quorum_probe_ifaces()}

    def quorum_tick(self, now: float | None = None) -> list[tuple]:
        """Такт голосующего (5 с): интерфейсы, пробы, взгляды → qview."""
        now = time.monotonic() if now is None else now
        if not self.quorum_present():
            self._gw_qvoters.clear()
            return []
        want = self.quorum_probes_apply()
        me = self.quorum_my_node()
        if not me:
            return []
        for name in list(self._gw_qvoters):
            if name not in want:
                self._gw_qvoters.pop(name, None)
        sec = self.quorum_section()
        timing = self.quorum_timing()
        actions: list[tuple] = []
        for name, rec in want.items():
            v = self._gw_qvoters.get(name)
            if v is None:
                v = Voter(str(rec["l"]), me, timing=timing)
                v.start(now)
                v.epoch_in(max(int(sec.get("e") or 0), int(sec.get("e_known") or 0)))
                self._gw_qvoters[name] = v
                self._pledge_restore(name, v, now)
            elif v.timing != timing:
                v.timing = timing                             # лидер сменил цифры — голосующий считает ими же
            for act in v.tick(now):
                if act == "probe":
                    p = Probe(str(rec["l"]), int(sec.get("g") or 1), me, int(v.epoch), v.w_for_probe(),
                              v.probe_out(now, qt.jitter(timing.probe_s)))
                    actions.append(("probe", name, p.encode(), (str(rec["resp"]), int(rec["port"]))))
                elif act in ("see", "uns", "sus"):
                    actions += self._quorum_view_changed(name, rec, v, act)
                elif act == "unpledge":
                    self._pledge_store(name, None)
                    log.info("quorum: обязательство «нет» снято по сроку")
            due = (self.__dict__.get("_quo_ask_due") or {}).get(name)
            if due is not None and now >= due:
                self.__dict__["_quo_ask_due"].pop(name, None)
                actions += self._quorum_qview_now(name, rec, v)      # ответ на qask после свежих проб
        online = bool(self.__dict__.get("_quo_reserve_online"))
        sess = self.__dict__.get("_quo_guest_session")             # номер сессии от runtime (обрыв между тактами)
        if online and (not self.__dict__.get("_quo_online_prev") or sess != self.__dict__.get("_quo_session_prev")):
            # новая сессия гостевого канала: текущий взгляд о сервере — первым словом (у резерва взгляды в памяти)
            for name, rec in want.items():
                v = self._gw_qvoters.get(name)
                if v is not None and rec.get("target") == "h" and v.view != "new":
                    actions += self._quorum_qview_now(name, rec, v)
        self.__dict__["_quo_online_prev"] = online
        if online:
            self.__dict__["_quo_session_prev"] = sess
        return actions

    def quorum_reply_in(self, name: str, data: bytes) -> list[tuple]:
        v = self._gw_qvoters.get(name)
        if v is None:
            return []
        r = Reply.parse(data)
        if r is None:
            return []
        now = time.monotonic()
        actions: list[tuple] = []
        for act in v.reply_in(r, now):
            if act == "see":
                rec = self.quorum_probe_ifaces().get(name) or {"l": v.l, "target": name[-1:]}
                actions += self._quorum_view_changed(name, rec, v, "see")
            elif act.startswith("adopt:"):
                # эпоха из ответа (клона у резерва — E после взятия) — факт линии: всем голосующим
                # и на диск; иначе пробы к основе дома шли бы с прежней и она набрала бы «да»
                self._quorum_epoch_in(int(v.epoch))
                log.info("quorum: ответ %s несёт эпоху %s — принята", "клона" if name.endswith("r") else "сервера AWG", v.epoch)
        return actions

    def _quorum_view_changed(self, name: str, rec: dict, v: Voter, w: str) -> list[tuple]:
        target = "клон" if rec.get("target") == "r" else "сервер AWG"
        log.info("quorum: %s — %s", target, {"see": "вижу", "sus": "ответ пропущен", "uns": "не вижу"}[w])
        with STORE_LOCK:
            cur = self.quorum_section()
            views = dict(cur.get("views") or {})
            views[name] = {"w": w, "since": timeutil.now_iso(), "target": str(rec.get("target") or "")}
            cur["views"] = views
            _write(cur, self._QUO_STORE)
        if w == "sus":
            return []
        body = {"l": str(rec.get("l") or ""), "g": int(cur.get("g") or 0), "w": w, "since": views[name]["since"], "e": int(v.epoch),
                "tgt": str(rec.get("target") or "h")}
        info = v.info()
        if info is not None and w == "see":
            body["i"] = info.to_dict()
        if w == "uns":
            v.said_no(time.monotonic())                       # «нет» кандидату (о сервере или о клоне) — обязательство под этой эпохой
            self._pledge_store(name, {"e": int(v.epoch), "at": timeutil.now_iso()})
        # взгляд о клоне — только своему серверу: резерв цель не различает, «не вижу клон» лёг бы взглядом на основу
        return ([("guest", "qview", body)] if rec.get("target") != "r" else []) + [("link", "qview", body), ("push",)]

    def quorum_guest_in(self, kind: str, body: dict) -> list[tuple]:
        """Слово кандидата (резерв по гостевому каналу или свой сервер по каналу
        конфигурации в отъезде, `tgt = r` — о своём клоне): `qstate` — эпоха линии; `qask` — опрос (взгляд сразу
        или после трёх свежих проб); `qtake{e}` — забирает под e + 1; `qrel{e}` —
        обязательство снято; `qneed` — точка ключа кворума, если сам не вижу основу и
        поколение совпало; `qpause{until}` — «не вижу» кандидату не уходит до срока."""
        sec = self.quorum_section()
        l = str(body.get("l") or "")
        if not sec.get("l") or (l and l != str(sec.get("l"))):
            return []
        now = time.monotonic()
        name = f"awgq{sec['l']}{'r' if str(body.get('tgt') or 'h') == 'r' else 'h'}"   # tgt = r — свой сервер в отъезде о клоне
        v = self._gw_qvoters.get(name)
        if kind == "qstate":
            e = int(body.get("e") or 0)
            if e:
                self._quorum_epoch_in(e)
            return []
        if v is None:
            return []
        if kind == "qask":
            return self._quorum_asked(name, v, now)
        if kind == "qtake":
            v.take(int(body.get("e") or 0))
            self._quorum_epoch_persist(int(v.epoch))
            self._pledge_store(name, None)
            log.warning("quorum: кандидат забирает основу под эпоху %s — обязательство снято", v.epoch)
            return []
        if kind == "qrel":
            if v.release(int(body.get("e") or 0)):
                self._pledge_store(name, None)
                log.info("quorum: обязательство «нет» под эпохой %s снято (qrel)", body.get("e"))
            if hasattr(self, "reserve_share_released"):
                self.reserve_share_released()                 # «не забираю» до начала попытки — точка не понадобится
            return []
        if kind == "qneed":
            return self._quorum_need_in(sec, v, int(body.get("q_gen") or 0), str(body.get("since") or ""))
        if kind == "qpause":
            try:
                secs = max(0.0, timeutil.elapsed(timeutil.now(), timeutil.parse_iso(str(body.get("until") or ""))))
            except (ValueError, TypeError):
                secs = 0.0
            v.pause(now, secs)
            log.info("quorum: сервер AWG просит паузу до %s", body.get("until"))
            return []
        return []

    def _quorum_asked(self, name: str, v: Voter, now: float) -> list[tuple]:
        """`qask`: взгляд uns/sus — qview сразу; see и ответ свежее fresh_s — qview{see, i}
        сразу; see со старым ответом — три пробы за ≤ 15 с, qview после них."""
        rec = self.quorum_probe_ifaces().get(name) or {"l": v.l, "target": "h"}
        if v.view in ("uns", "sus") or (v.view == "see" and v.last_reply and now - v.last_reply <= v.timing.fresh_s):
            return self._quorum_qview_now(name, rec, v)
        v.next_probe = now
        v.resends = 0
        self.__dict__.setdefault("_quo_ask_due", {})[name] = now + 15.0
        log.info("quorum: опрос кандидата — три свежие пробы основы")
        return []

    def _quorum_qview_now(self, name: str, rec: dict, v: Voter) -> list[tuple]:
        w = v.view if v.view != "sus" else "uns"
        cur = self.quorum_section()
        body = {"l": str(rec.get("l") or ""), "g": int(cur.get("g") or 0), "w": w,
                "since": str((cur.get("views") or {}).get(name, {}).get("since") or timeutil.now_iso()), "e": int(v.epoch),
                "tgt": str(rec.get("target") or "h")}
        info = v.info()
        if info is not None and w == "see":
            body["i"] = info.to_dict()
        if w == "uns":
            v.said_no(time.monotonic())
            self._pledge_store(name, {"e": int(v.epoch), "at": timeutil.now_iso()})
        return ([("guest", "qview", body)] if rec.get("target") != "r" else []) + [("link", "qview", body)]

    def _quorum_need_in(self, sec: dict, v: Voter, q_gen: int, since: str) -> list[tuple]:
        point = sec.get("point") or {}
        if v.view != "uns":
            log.info("quorum: кандидат просит точку (с %s), а сервер AWG виден — отказ", since)
            return []
        if not point.get("y") or int(point.get("q_gen") or 0) != int(q_gen):
            log.warning("quorum: кандидат просит точку поколения %s, у меня %s — отказ", q_gen, point.get("q_gen"))
            return []
        log.warning("quorum: отдаю точку ключа кворума кандидату (сервер AWG не отвечает с %s)", since)
        from .store import _write as _write_rsv
        with STORE_LOCK:
            rs = self.reserve_section()                       # отметку читает слово state{idle, reason} — Т-РА5
            rs["share_given_at"], rs["share_move"] = timeutil.now_iso(), None   # попытку свяжет первый serving
            _write_rsv(rs, self._RSV_STORE)
        return [("guest", "qshare", {"l": str(sec.get("l") or ""), "q_gen": int(q_gen), "x": int(point.get("x") or 0),
                                     "y": str(point.get("y"))})]

    def _pledge_store(self, name: str, pledge: dict | None) -> None:
        with STORE_LOCK:
            cur = self.quorum_section()
            pledges = dict(cur.get("pledges") or {})
            if pledge is None:
                pledges.pop(name, None)
            else:
                pledges[name] = pledge
            cur["pledges"] = pledges
            _write(cur, self._QUO_STORE)

    def _pledge_restore(self, name: str, v: Voter, now: float) -> None:
        """Обязательство «нет» переживает перезапуск агента (срок — по отметке)."""
        pledge = (self.quorum_section().get("pledges") or {}).get(name)
        if not isinstance(pledge, dict):
            return
        try:
            age = timeutil.elapsed(timeutil.parse_iso(str(pledge.get("at") or "")))
        except (ValueError, TypeError):
            age = v.timing.pledge_s + 1
        if age < v.timing.pledge_s:
            v.pledge = {"e": int(pledge.get("e") or 0), "at": now - age}
            if int(pledge.get("e") or 0) > v.epoch:
                v.epoch = int(pledge.get("e") or 0)
        else:
            self._pledge_store(name, None)

    # ── панель и снимок ─────────────────────────────────────────────────────
    def quorum_panel_view(self) -> dict:
        """Т-К17: {name, w, since, y, v} по зонду к основе; пусто — раздела нет."""
        sec = self.quorum_section()
        if not sec.get("l"):
            return {}
        name = f"awgq{sec['l']}h"
        v = self._gw_qvoters.get(name)
        view = dict((sec.get("views") or {}).get(name) or {})
        out = {"name": str((self.reserve_section() if hasattr(self, "reserve_section") else {}).get("name") or ""),
               "w": v.view if v is not None else str(view.get("w") or ""), "since": str(view.get("since") or ""),
               "reserve_online": bool(getattr(self, "_quo_reserve_online", False))}
        if v is not None and v.last is not None:
            out.update(y=int(v.last.y), v=int(v.last.v), r=str(v.last.r))
        return out

    def quorum_snapshot(self) -> dict | None:
        """Поле `quo` снимка: взгляд на основу, поколение, эпоха."""
        sec = self.quorum_section()
        if not sec.get("l"):
            return None
        name = f"awgq{sec['l']}h"
        v = self._gw_qvoters.get(name)
        view = dict((sec.get("views") or {}).get(name) or {})
        return {"w": v.view if v is not None else str(view.get("w") or ""), "since": str(view.get("since") or ""),
                "g": int(sec.get("g") or 0), "e": int(v.epoch) if v is not None else int(sec.get("e") or 0),
                "q": int((sec.get("point") or {}).get("q_gen") or 0)}      # поколение моей точки — подтверждение ключей
