"""listsnap.py — снимки событий для экрана «весь список» (bot/lists, вид snap).

Итог действия, отчёт или уведомление, чей список не влез, кладут его сюда
целиком готовыми строками HTML, а в тексте оставляют хвост «и ещё N» ссылкой
`list-snap-<ключ>`. Снимок — в `server_state` под ключом `list_snap:<ключ>`
(у основного бота и у агента — своя база):

    {"v": 1, "chat": …, "role": "admin", "at": iso, "seq": n, "title": "…", "n": N,
     "rows": […], "back": "<колбэк>", "new": false}

`role` и `new` — справочные (кто создал, ушла ли ссылка с меткой «-n»): экран
берёт роль того, кто смотрит, а метку — из самой ссылки.

Ключ — 10 знаков [a-z0-9] из secrets (не перебирается, не совпадает со старым
после восстановления базы); открыть снимок можно только из чата, где он
создан. Срок — 30 дней, в чате — не больше 20 снимков (вытесняется самый
ранний по порядковому номеру `seq`), в снимке — не больше 2000 строк и
256 КБ; сверх (или когда источник сам отдал не все — `missing`) — последняя
строка «… и ещё N — полный список в журнале бота», а в журнал — заголовок,
числа и отрезанный хвост (с пределом длины). N в шапке — настоящее число
элементов от производителя (`n`), а не строк: строк может быть меньше
(свёрнутые в одну), и это не обрезка.

Протухший или вытесненный снимок оставляет отметку `{"chat", "at", "gone": 1}`
(десятки байт, держится TOMB_TTL_S): ссылка на него отвечает «список больше не
хранится», а не «его нет». Уборка ленивая — при создании и при открытии.
Запись не роняет вызывающего: сбой — строкой журнала и пустой ключ (хвост
останется текстом). В бэкап снимки не идут (domain/backup/archive), в реплику
резерва — тоже (белый список состояния).
"""
from __future__ import annotations

import json
import logging
import secrets
import string

from awgbot.util import timeutil

PREFIX = "list_snap:"
TTL_S = 30 * 86400
TOMB_TTL_S = 90 * 86400
PER_CHAT = 20
TOMBS_PER_CHAT = 50
MAX_ROWS = 2000
MAX_BYTES = 256 * 1024
LOG_TAIL_ROWS = 50
LOG_ROW_CHARS = 200
_ALPHABET = string.ascii_lowercase + string.digits
_WHERE = "substr(key, 1, 10) = 'list_snap:'"      # не LIKE: «_» там — подстановка

log = logging.getLogger("awgbot.db")


def new_key() -> str:
    """Ключ снимка: 10 знаков [a-z0-9] из secrets (≈ 52 бита)."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(10))


class ListSnapMixin:

    def _snaps(self) -> list[tuple[str, dict]]:
        out = []
        for row in self._connection().execute(f"SELECT key, value FROM server_state WHERE {_WHERE}").fetchall():
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

    @staticmethod
    def _tomb(data: dict) -> str:
        return json.dumps({"chat": int(data.get("chat") or 0), "at": str(data.get("at") or ""), "gone": 1})

    def _snap_sweep(self, snaps: list[tuple[str, dict]], chat: int) -> None:
        """Протухшие — в отметки; в чате chat — живых не больше PER_CHAT − 1 (место под
        новый, вытесняется самый ранний), отметок — не больше TOMBS_PER_CHAT; старые
        отметки — прочь."""
        tomb, drop = [], []
        live_mine, tombs_mine = [], []
        for k, d in snaps:
            if d.get("gone"):
                if self._snap_age(d) > TOMB_TTL_S:
                    drop.append(k)
                elif int(d.get("chat") or 0) == int(chat):
                    tombs_mine.append((str(d.get("at") or ""), k))
                continue
            if self._snap_age(d) > TTL_S:
                tomb.append((k, d))
            elif int(d.get("chat") or 0) == int(chat):
                live_mine.append((int(d.get("seq") or 0), k, d))
        live_mine.sort(key=lambda x: x[0])
        while len(live_mine) > PER_CHAT - 1:
            _s, k, d = live_mine.pop(0)
            tomb.append((k, d))
        new_tombs = sum(1 for _k, d in tomb if int(d.get("chat") or 0) == int(chat))
        tombs_mine.sort()
        while tombs_mine and len(tombs_mine) + new_tombs > TOMBS_PER_CHAT:
            drop.append(tombs_mine.pop(0)[1])
        if tomb or drop:
            with self._tx() as c:
                c.executemany("UPDATE server_state SET value = ? WHERE key = ?",
                              [(self._tomb(d), PREFIX + k) for k, d in tomb])
                c.executemany("DELETE FROM server_state WHERE key = ?", [(PREFIX + k,) for k in drop])
                self._tx_note(self._TX_COUNTERS)

    def list_snap_keep(self, chat: int, role: str, title: str, rows: list[str], back: str = "",
                       new: bool = False, n: int | None = None, missing: int = 0) -> str:
        """Положить снимок списка; вернуть его ключ (пусто — не записан: строка журнала,
        хвост у вызывающего останется текстом). n — сколько элементов на деле (шапка);
        missing — сколько их источник не передал строками (в журнале — у него)."""
        try:
            return self._snap_keep(chat, role, title, rows, back, new, n, max(0, int(missing or 0)))
        except Exception as e:                                # noqa: BLE001 — снимок не должен ронять итог
            log.error("lists: снимок «%s» не записан: %s", title, e)
            return ""

    def _snap_keep(self, chat, role, title, rows, back, new, n, missing) -> str:
        rows = [str(r) for r in rows]
        snaps = self._snaps()
        seq = max((int(d.get("seq") or 0) for _k, d in snaps), default=0) + 1
        body = {"v": 1, "chat": int(chat), "role": str(role), "at": timeutil.now_iso(), "seq": seq,
                "title": str(title), "n": int(n if n is not None else len(rows)), "rows": rows,
                "back": str(back or ""), "new": bool(new)}
        total = len(rows) + missing                          # источник мог отдать не все
        keep = min(len(rows), MAX_ROWS)
        if keep < total and keep == MAX_ROWS:
            keep -= 1                                         # место под строку «и ещё N»
        while True:
            tail = [f"… и ещё {total - keep} — полный список в журнале бота"] if keep < total else []
            body["rows"] = rows[:keep] + tail
            raw = json.dumps(body, ensure_ascii=False)
            if len(raw.encode()) <= MAX_BYTES or keep <= 1:
                break
            keep = max(1, keep - max(1, keep // 10))          # не влезло по объёму — короче на десятую
        if keep < len(rows):
            cut = [r[:LOG_ROW_CHARS] for r in rows[keep:keep + LOG_TAIL_ROWS]]
            log.info("lists: «%s» — %d строк, в снимок вошли %d; не вошли (первые %d): %s", title, len(rows), keep,
                     len(cut), cut)
        self._snap_sweep(snaps, int(chat))
        have = {k for k, _d in snaps}
        key = new_key()
        while key in have:
            key = new_key()
        with self._tx() as c:
            c.execute("INSERT INTO server_state (key, value) VALUES (?, ?)", (PREFIX + key, raw))
            self._tx_note(self._TX_COUNTERS)
        return key

    def list_snap_get(self, key: str, chat: int) -> dict | None:
        """Снимок по ключу из этого чата; протухший или вытесненный — {"stale": True, "at": …};
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
        if data.get("gone"):
            return {"stale": True, "at": str(data.get("at") or "")}
        if self._snap_age(data) > TTL_S:
            with self._tx() as c:
                c.execute("UPDATE server_state SET value = ? WHERE key = ?", (self._tomb(data), PREFIX + str(key)))
                self._tx_note(self._TX_COUNTERS)
            return {"stale": True, "at": str(data.get("at") or "")}
        return data
