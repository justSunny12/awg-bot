"""screen.py — часть пакета rfpeer: данные строк карточки обеих сторон и экрана роли."""
from __future__ import annotations

from .slot import RF_PEER_ID


class ReserveRfScreenMixin:

    def reserve_rfpeer_view(self) -> dict:
        """Для карточки «🛟 Резерв»: {use: {since}, offer_missing: причина, probe_down: since,
        peer_use: {since, up, dn}, offer: bool}."""
        if not self.reserve_paired():
            return {}
        cur = self.rsv_get("rsv_rfpeer")
        out: dict = {"offer": bool((cur.get("offer") or {}).get("on"))}
        use = dict(cur.get("use") or {})
        if use:
            out["use"] = {"since": str(use.get("since") or "")}
        elif self.db.gateways():
            why = self.reserve_rf_no_backup_why() if hasattr(self, "reserve_rf_no_backup_why") else ""
            if why:
                out["offer_missing"] = why
                out["offer_missing_code"] = self.reserve_rf_no_backup_code()   # ☑️ — выключенная настройка, иначе ⚠️
            elif cur.get("slot"):
                try:
                    if self._rt_dead(RF_PEER_ID):
                        out["probe_down"] = str(self.db.get_state(f"routing_gw_{RF_PEER_ID}_down_since") or "")
                except Exception:                             # noqa: BLE001
                    pass
        ep = self.reserve_rf_peer_episode() if hasattr(self, "reserve_rf_peer_episode") else {}
        if ep:
            out["peer_use"] = ep
        return out
