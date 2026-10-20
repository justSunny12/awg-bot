"""
state.py — часть пакета reserve (см. __init__): пара в БД и ключи `rsv_*`
состояния, состояния направлений.

Пара — строка `reserve_peer` (одна). Всё остальное о паре — ключи
`server_state` с началом `rsv_` (перечень — KEYS): JSON-объекты, читаются и
пишутся через `rsv_get`/`rsv_put`. В пакет переезда они не едут никогда
(сторож `util/rsvpkg/bot.FORBIDDEN_STATE`).

Направления: `rsv_out` — мои клиенты (`home` / `leaving` / `away` /
`returning`), `rsv_in` — клиенты резерва у меня (`idle` / `taking` /
`serving` / `releasing` / `relay`), `rsv_poll` — право отвечать в Telegram
(`mine` / `away`).
"""
from __future__ import annotations

import json
import threading

from awgbot.util import timeutil

KEYS: tuple[str, ...] = (
    "rsv_invite", "rsv_peer_state", "rsv_out", "rsv_in", "rsv_poll", "rsv_keys", "rsv_view",
    "rsv_shares", "rsv_pkg_due", "rsv_pkg_sent", "rsv_pkg_in", "rsv_peer_admin", "rsv_guest_links",
    "rsv_gw_links", "rsv_traffic", "rsv_dns", "rsv_record_watch",
)
OUT_STATES = ("home", "leaving", "away", "returning")
IN_STATES = ("idle", "taking", "serving", "releasing", "relay")
POLL_STATES = ("mine", "away")

_LOCK = threading.RLock()


class ReserveStateMixin:

    # ── ключи состояния ─────────────────────────────────────────────────────

    def rsv_get(self, key: str) -> dict:
        if key not in KEYS:
            raise KeyError(f"не ключ резерва: {key}")
        raw = self.db.get_state(key) or ""
        try:
            data = json.loads(raw) if raw.startswith("{") else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def rsv_put(self, key: str, data: dict | None) -> None:
        if key not in KEYS:
            raise KeyError(f"не ключ резерва: {key}")
        self.db.set_state(key, json.dumps(data, ensure_ascii=False) if data else "")

    def rsv_update(self, key: str, **fields) -> dict:
        with _LOCK:
            cur = self.rsv_get(key)
            cur.update(fields)
            self.rsv_put(key, cur)
            return cur

    def rsv_clear_all(self) -> None:
        """Удаление пары: все ключи резерва прочь (кроме месячного итога в таблице)."""
        for k in KEYS:
            self.rsv_put(k, None)

    # ── пара ─────────────────────────────────────────────────────────────────

    def reserve_peer(self) -> dict | None:
        return self.db.reserve_peer()

    def reserve_paired(self) -> bool:
        return self.reserve_peer() is not None

    def reserve_peer_name(self) -> str:
        p = self.reserve_peer()
        return (p or {}).get("name") or "резерв"

    def reserve_peer_state(self) -> dict:
        """Последний `st` резерва о себе (версия, возможности, готовность, порты, голосующие)."""
        return self.rsv_get("rsv_peer_state")

    # ── направления ─────────────────────────────────────────────────────────

    def reserve_out(self) -> str:
        st = self.rsv_get("rsv_out").get("state") or "home"
        return st if st in OUT_STATES else "home"

    def reserve_in(self) -> str:
        st = self.rsv_get("rsv_in").get("state") or "idle"
        return st if st in IN_STATES else "idle"

    def reserve_poll(self) -> str:
        st = self.rsv_get("rsv_poll").get("state") or "mine"
        return st if st in POLL_STATES else "mine"

    def reserve_set_out(self, state: str, **why) -> dict:
        if state not in OUT_STATES:
            raise ValueError(f"не состояние направления: {state}")
        return self.rsv_update("rsv_out", state=state, since=timeutil.now_iso(), **why)

    def reserve_set_in(self, state: str, **why) -> dict:
        if state not in IN_STATES:
            raise ValueError(f"не состояние направления: {state}")
        return self.rsv_update("rsv_in", state=state, since=timeutil.now_iso(), **why)

    def reserve_set_poll(self, state: str, **why) -> dict:
        if state not in POLL_STATES:
            raise ValueError(f"не право ответа: {state}")
        return self.rsv_update("rsv_poll", state=state, since=timeutil.now_iso(), **why)

    def reserve_busy(self) -> str:
        """Идёт переезд или возврат (в любую сторону) — человеку словами; пусто — нет."""
        out, inn = self.reserve_out(), self.reserve_in()
        if out in ("leaving", "returning"):
            return "идёт переезд клиентов этого сервера" if out == "leaving" else "идёт возврат клиентов на этот сервер"
        if inn in ("taking", "releasing"):
            return "принимаю клиентов резерва" if inn == "taking" else "возвращаю клиентов резерву"
        return ""

    def reserve_hosting(self) -> bool:
        """У меня сейчас клиенты и бот резерва."""
        return self.reserve_in() in ("taking", "serving", "releasing")

    def reserve_away(self) -> bool:
        """Мои клиенты и бот — у резерва."""
        return self.reserve_out() in ("leaving", "away", "returning")
