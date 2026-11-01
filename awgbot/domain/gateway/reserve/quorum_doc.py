"""
quorum_doc.py — часть пакета reserve агента (см. __init__): кворум резерва на
шлюзе — раздел `quorum` документа конфигурации (файл `/var/lib/awg-gw/quorum.json`,
0600), его приём и снятие, интерфейсы зондов (`awgq<l>h` всегда, `awgq<l>r` —
пока пользователи основы у резерва) и цифры лидера (вынесено из quorum: там
пробы, взгляды и слова кандидата). Свои отметки хода (`_KEEP_KEYS`) новый
документ не стирает; точка прежнего поколения (`point_prev`) живёт до следующей
смены.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from awgbot.util import fsatomic, rsvkeys, timeutil
from awgbot.util.quorum.timing import Timing

from ..base import log
from .store import STORE_LOCK                                 # один замок на reserve.json и quorum.json

STORE_PATH = Path("/var/lib/awg-gw/quorum.json")
_KEEP_KEYS = ("views", "pledges", "told", "apply_mono", "share_given_at", "e_known", "e_take", "point_given")


def _read(path: Path = STORE_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict, path: Path = STORE_PATH) -> None:
    fsatomic.write_private(path, json.dumps(data, ensure_ascii=False))


def _point_prev(cur: dict, new_point: dict) -> dict:
    """Точка прежнего многочлена — после смены поколения точки: второй сервер получает свои
    точки и ящики (`qkeys`) только по живому линку, документ шлюзу приходит раньше; кандидат
    в этом окне просит точку своего (прежнего) поколения. Держится одно прежнее поколение;
    без точки (голосование выключено) — и прежней нет."""
    if not new_point.get("y"):
        return {}
    old = dict(cur.get("point") or {})
    if old.get("y") and not rsvkeys.same_q(old, new_point):
        return old
    prev = dict(cur.get("point_prev") or {})
    return prev if prev.get("y") and not rsvkeys.same_q(prev, new_point) else {}


def _point_of_gen(sec: dict, want: dict) -> dict:
    """Точка того поколения (`q_gen`, `qf`), которое просит кандидат: текущая или прежняя;
    иначе — текущая (отказ по несовпадению скажет, какое у меня)."""
    point = dict(sec.get("point") or {})
    prev = dict(sec.get("point_prev") or {})
    if not rsvkeys.same_q(point, want) and prev.get("y") and rsvkeys.same_q(prev, want):
        return prev
    return point


class GatewayQuorumDocMixin:
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
            prev = _point_prev(cur, new["point"])
            if prev:
                new["point_prev"] = prev
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

    # ── интерфейсы зондов и цифры лидера ────────────────────────────────────
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

    def quorum_runtime_plan(self) -> dict:
        return {"responder": None, "probes": self.quorum_probe_ifaces()}

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
