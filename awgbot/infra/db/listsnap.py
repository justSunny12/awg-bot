"""listsnap.py — снимки событий для экрана «весь список» (bot/lists, вид snap).

Итог действия, отчёт или уведомление, чей список не влез, кладут его сюда
целиком готовыми строками HTML, а в тексте оставляют хвост «и ещё N» ссылкой
`list-snap-<ключ>`. Снимок — в `server_state` под ключом `list_snap:<ключ>`
(у основного бота и у агента — своя база):

    {"v": 1, "chat": …, "role": "admin", "at": iso, "title": "…", "rows": […], "back": "<колбэк>", "new": false}

Ключ — 10 знаков [a-z0-9] из secrets (не перебирается, не совпадает со старым
после восстановления базы); открыть снимок можно только из чата, где он
создан. Срок — 30 дней, в чате — не больше 20 снимков (старейший
вытесняется), в снимке — не больше 2000 строк и 256 КБ; сверх — последняя
строка «… и ещё N — полный список в журнале бота», сам список — строкой
журнала. Уборка ленивая — при создании и при открытии. В бэкап снимки не идут
(domain/backup/archive), в реплику резерва — тоже (белый список состояния).
"""
from __future__ import annotations

import json
import logging
import secrets
import string

from awgbot.util import timeutil

PREFIX = "list_snap:"
TTL_S = 30 * 86400
PER_CHAT = 20
MAX_ROWS = 2000
MAX_BYTES = 256 * 1024
_ALPHABET = string.ascii_lowercase + string.digits

log = logging.getLogger("awgbot.db")


def new_key() -> str:
    """Ключ снимка: 10 знаков [a-z0-9] из secrets (≈ 52 бита)."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(10))


class ListSnapMixin:

    def _snaps(self) -> list[tuple[str, dict]]:
        out = []
        for row in self._connection().execute("SELECT key, value FROM server_state WHERE key LIKE ?",
                                              (PREFIX + "%",)).fetchall():
            try:
                data = json.loads(row[1] or "{}")
            except ValueError:
                data = {}
            out.append((str(row[0])[len(PREFIX):], data if isinstance(data, dict) else {}))
        return out

    @staticmethod
    def _snap_age(data: dict) -> float:
        try:
            return timeutil.elapsed(timeutil.parse_iso(str(data.get("at") or "")))
        except ValueError:
            return float("inf")

    def _snap_sweep(self, chat: int = 0) -> None:
        """Протухшие — прочь; в чате chat — не больше PER_CHAT − 1 (место под новый)."""
        snaps = self._snaps()
        drop = [k for k, d in snaps if self._snap_age(d) > TTL_S]
        if chat:
            mine = sorted((d.get("at") or "", k) for k, d in snaps if int(d.get("chat") or 0) == int(chat) and k not in drop)
            while len(mine) > PER_CHAT - 1:
                drop.append(mine.pop(0)[1])
        if drop:
            with self._tx() as c:
                c.executemany("DELETE FROM server_state WHERE key = ?", [(PREFIX + k,) for k in drop])
                self._tx_note(self._TX_COUNTERS)

    def list_snap_keep(self, chat: int, role: str, title: str, rows: list[str], back: str = "",
                       new: bool = False) -> str:
        """Положить снимок списка; вернуть его ключ. Сверх предела — начало списка и
        строка «… и ещё N — полный список в журнале бота» (сам список — в журнал)."""
        rows = [str(r) for r in rows]
        body = {"v": 1, "chat": int(chat), "role": str(role), "at": timeutil.now_iso(), "title": str(title),
                "rows": rows, "back": str(back or ""), "new": bool(new)}
        keep = min(len(rows), MAX_ROWS)
        if keep < len(rows):
            keep -= 1                                         # место под строку «и ещё N»
        while True:
            tail = [f"… и ещё {len(rows) - keep} — полный список в журнале бота"] if keep < len(rows) else []
            body["rows"] = rows[:keep] + tail
            raw = json.dumps(body, ensure_ascii=False)
            if len(raw.encode()) <= MAX_BYTES or keep <= 1:
                break
            keep = max(1, keep - max(1, keep // 10))          # не влезло по объёму — короче на десятую
        if keep < len(rows):
            log.info("список «%s» — %d строк, в снимок вошли %d; целиком: %s", title, len(rows), keep, rows)
        self._snap_sweep(int(chat))
        have = {k for k, _d in self._snaps()}
        key = ""
        while not key or key in have:
            key = new_key()
        with self._tx() as c:
            c.execute("INSERT INTO server_state (key, value) VALUES (?, ?)", (PREFIX + key, raw))
            self._tx_note(self._TX_COUNTERS)
        return key

    def list_snap_get(self, key: str, chat: int) -> dict | None:
        """Снимок по ключу из этого чата; протухший — {"stale": True, "at": …} (и прочь);
        нет или чужой — None."""
        row = self._connection().execute("SELECT value FROM server_state WHERE key = ?", (PREFIX + str(key),)).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row[0] or "{}")
        except ValueError:
            return None
        if not isinstance(data, dict) or int(data.get("chat") or 0) != int(chat):
            return None                                      # чужой чат — как несуществующий
        if self._snap_age(data) > TTL_S:
            with self._tx() as c:
                c.execute("DELETE FROM server_state WHERE key = ?", (PREFIX + str(key),))
                self._tx_note(self._TX_COUNTERS)
            return {"stale": True, "at": str(data.get("at") or "")}
        return data
