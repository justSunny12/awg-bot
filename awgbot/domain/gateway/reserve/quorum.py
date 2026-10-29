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

from awgbot.util import livestep, timeutil
from awgbot.util.quorum import timing as qt
from awgbot.util.quorum.probe import Probe, Reply
from awgbot.util.quorum.timing import Timing
from awgbot.util.quorum.voter import Voter

from ..base import log

STORE_PATH = Path("/var/lib/awg-gw/quorum.json")
_KEEP_KEYS = ("views", "pledges", "told", "apply_mono")
APPLY_EVERY_S = 300


def _read(path: Path = STORE_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict, path: Path = STORE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))


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
        cur = self.quorum_section()
        if checked is None:
            if not cur:
                return {"changed": False, "note": ""}
            self.quorum_remove()
            return {"changed": True, "note": "🗳 Голосование о сервере AWG снято — зонды опущены"}
        new = {"l": str(checked.get("l") or ""), "g": int(checked.get("g") or 0),
               "members": list(checked.get("members") or []), "t": int(checked.get("t") or 0),
               "port": int(checked.get("port") or 0), "probes": dict(checked.get("probes") or {}),
               "point": dict(checked.get("point") or {}), "e": int(checked.get("e") or 0),
               "applied_at": timeutil.now_iso()}
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
                out = quoprobe.up(name, rec["conf"])
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
        from awgbot.core import settings
        q = settings.get("app.gateway.quorum", {}) or {}
        return Timing.from_settings(q if isinstance(q, dict) else {}, settings.get_int("app.gateway.rf_block_minutes", 15))

    def quorum_my_node(self) -> str:
        sec = self.quorum_section()
        slot = int((self.reserve_section() if hasattr(self, "reserve_section") else {}).get("slot") or 0)
        return f"{sec.get('l')}{slot}" if sec.get("l") and slot else ""

    def _quorum_epoch_in(self, e: int) -> None:
        for v in self._gw_qvoters.values():
            v.epoch_in(int(e))

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
                v.epoch_in(int(sec.get("e") or 0))
                self._gw_qvoters[name] = v
            for act in v.tick(now):
                if act == "probe":
                    p = Probe(str(rec["l"]), int(sec.get("g") or 1), me, int(v.epoch), v.w_for_probe(),
                              v.probe_out(now, qt.jitter(timing.probe_s)))
                    actions.append(("probe", name, p.encode(), (str(rec["resp"]), int(rec["port"]))))
                elif act in ("see", "uns", "sus"):
                    actions += self._quorum_view_changed(name, rec, v, act)
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
        return actions

    def _quorum_view_changed(self, name: str, rec: dict, v: Voter, w: str) -> list[tuple]:
        target = "клон" if rec.get("target") == "r" else "сервер AWG"
        log.info("quorum: %s — %s", target, {"see": "вижу", "sus": "ответ пропущен", "uns": "не вижу"}[w])
        cur = self.quorum_section()
        views = dict(cur.get("views") or {})
        views[name] = {"w": w, "since": timeutil.now_iso(), "target": str(rec.get("target") or "")}
        cur["views"] = views
        _write(cur, self._QUO_STORE)
        if w == "sus":
            return []
        body = {"l": str(rec.get("l") or ""), "g": int(cur.get("g") or 0), "w": w, "since": views[name]["since"], "e": int(v.epoch)}
        info = v.info()
        if info is not None and w == "see":
            body["i"] = info.to_dict()
        return [("guest", "qview", body), ("link", "qview", body), ("push",)]

    def quorum_guest_in(self, kind: str, body: dict) -> list[tuple]:
        """Слово резерва по гостевому каналу о кворуме: `qstate` — эпоха линии; опрос
        и слова кандидата (`qask`, `qtake`, `qrel`, `qneed`, `qpause`) — этап решения."""
        if kind == "qstate":
            e = int(body.get("e") or 0)
            if e:
                self._quorum_epoch_in(e)
        return []

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
                "g": int(sec.get("g") or 0), "e": int(v.epoch) if v is not None else int(sec.get("e") or 0)}
