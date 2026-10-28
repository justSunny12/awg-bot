"""
tail_notes.py — часть пакета journal (см. __init__): уведомления по хвосту
старой эпохи — только у основы (tail, c9).

Т-Ж4 (спорное): первые TAIL_LINES_MAX строк по записям отчёта перебазирования
(rsvjrn.rebase) — профиль и устройство ссылками или экранированным текстом, кто
занял уникальное; весь список — снимком в базе бота основы (он переживает
`--wipe` резерва), хвост — ссылкой на него. Т-Ж5 (всё перенесено) — числом.
Тексты — notices/reserve_tail; вариант: `crash` — мой хвост после аварии,
`c9` — правки бота на резерве, `self` — свой хвост перенесён здесь.
"""
from __future__ import annotations

from awgbot.core import config

from ...types import Notification


class ReserveTailNotesMixin:

    def _tail_notes(self, variant: str, moved: int, disputed: list[dict], total: int) -> list[Notification]:
        from awgbot.domain.notices import reserve_tail as rt
        name = self.reserve_peer_name()
        if not disputed and not total:
            if not moved:
                return []
            return [Notification(tg_id=int(config.ADMIN_ID), text=rt.tail_moved(variant, name, moved), silent=True)]
        total = max(total, len(disputed))
        rows = [self._tail_line(d, variant) for d in disputed]
        lines = rows[:rt.TAIL_LINES_MAX]
        more = ""
        if total > rt.TAIL_LINES_MAX:
            # весь список — снимком в базе бота основы (переживает --wipe резерва), хвост — ссылкой на него
            from awgbot.util import listlink
            from awgbot.util.textfmt.tail import more_line
            key = self.db.list_snap_keep(int(config.ADMIN_ID), "admin", rt.tail_snap_title(variant, name), rows,
                                         new=True, n=total, missing=total - len(rows))
            if key:
                more = more_line(total - len(lines), str(getattr(self, "bot_username", "") or ""),
                                 listlink.build("snap", key=key))
        return [Notification(tg_id=int(config.ADMIN_ID), text=rt.tail_disputed(variant, name, lines, total, more),
                             silent=True)]

    def _tail_line(self, d: dict, variant: str) -> str:
        from awgbot.domain.notices import reserve_tail as rt
        from awgbot.util.textfmt.base import profile_link
        import html
        bot = str(getattr(self, "bot_username", "") or "")
        client = None
        try:
            if d.get("client_id") is not None:
                client = self.db.get_client(int(d["client_id"]))
        except Exception:                                     # noqa: BLE001 — строка и без владельца
            client = None
        device = ""
        if client is not None:
            profile = profile_link(client, bot)
        else:                                                 # владелец не определён: у строки профиля — её имя
            profile = "" if d.get("table") == "devices" else html.escape(str(d.get("name") or "?"), quote=False)
        if d.get("table") == "devices":
            tail_name = (d.get("tail") or {}).get("name") if isinstance(d.get("tail"), dict) else ""
            device = html.escape(str(d.get("name") or tail_name or "?"), quote=False)
        taken = ""
        tb = d.get("taken_by") or {}
        if tb:
            owner = None
            try:
                owner = self.db.get_client(int(tb.get("client_id") or 0))
            except Exception:                                 # noqa: BLE001
                owner = None
            taken = f"«{html.escape(str(tb.get('name') or '?'), quote=False)}»" + \
                (f" профиля {profile_link(owner, bot)}" if owner is not None else "")
        return rt.tail_line(d, profile, device, taken, self.reserve_peer_name(), variant)

    def reserve_tail_notes(self) -> list[Notification]:
        """После возврата: Т-Ж4/Т-Ж5 по хвосту этой базы, перебазированному ботом на резерве
        (вариант «crash»: хвост после аварии)."""
        rep = self.rsv_get("rsv_out").get("tail_report") or {}
        if not rep:
            return []
        self.rsv_update("rsv_out", tail_report=None)
        return self._tail_notes("crash", int(rep.get("moved") or 0), list(rep.get("disputed") or []),
                                int(rep.get("total") or 0))
