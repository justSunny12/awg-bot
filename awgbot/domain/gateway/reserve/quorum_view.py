"""
quorum_view.py — часть пакета reserve агента (см. __init__): кворум резерва на
шлюзе — свой взгляд зонда на сервер AWG, строка панели (Т-К17) и поле `quo`
снимка (вынесено из quorum).
"""
from __future__ import annotations

from awgbot.util import rsvkeys


class GatewayQuorumViewMixin:
    # ── свой взгляд, панель и снимок ────────────────────────────────────────
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
        """Поле `quo` снимка: взгляд на основу, поколение, эпоха; `pg` — точка какого многочлена
        и когда отдана кандидату (основа по нему узнаёт о возможно известном ключе, даже если
        резерв промолчал)."""
        sec = self.quorum_section()
        if not sec.get("l"):
            return None
        name = f"awgq{sec['l']}h"
        v = self._gw_qvoters.get(name)
        view = dict((sec.get("views") or {}).get(name) or {})
        out = {"w": v.view if v is not None else str(view.get("w") or ""), "since": str(view.get("since") or ""),
               "g": int(sec.get("g") or 0), "e": int(v.epoch) if v is not None else int(sec.get("e") or 0),
               "q": int((sec.get("point") or {}).get("q_gen") or 0)}      # поколение моей точки — подтверждение ключей
        qf = rsvkeys.m_print_of((sec.get("point") or {}).get("qf"))
        if qf:
            out["qf"] = qf                                    # и её многочлен: номер после копии основы не уникален
        pg = sec.get("point_given")
        if isinstance(pg, dict) and int(pg.get("q") or 0):
            out["pg"] = {"q": int(pg["q"]), "at": str(pg.get("at") or "")}
            if rsvkeys.m_print_of(pg.get("qf")):
                out["pg"]["qf"] = rsvkeys.m_print_of(pg.get("qf"))
        return out
