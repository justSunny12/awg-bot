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
256 КБ; сверх — последняя строка «… и ещё N — полный список в журнале бота»,
и отрезанное целиком уходит в журнал порциями по LOG_TAIL_ROWS строк (строка —
до LOG_ROW_CHARS знаков), каждая строка журнала — с ключом снимка и чатом.
Когда источник сам отдал не все (`missing`), в журнале их нет, и хвост
говорит это («… и ещё N — в снимок не вошли»); обрезало и хранилище — обе
части («… и ещё N — K в снимок не вошли, остальные — в журнале бота»). Текст
вызывающего (`tail_note`, когда остаток лежит не в журнале этого бота: Т-Ж4
после аварии — в журнале бота на резерве) заменяет оба варианта. N в шапке — настоящее число
элементов от производителя (`n`), а не строк: строк может быть меньше
(свёрнутые в одну), и это не обрезка. Одна строка больше 256 КБ — снимок не
пишется (сбой записи, хвост у вызывающего остаётся текстом).

Протухший или вытесненный снимок оставляет отметку `{"chat", "at", "gone": 1,
"why": ""|"evicted"}` (десятки байт, держится TOMB_TTL_S): ссылка на него
отвечает «список больше не хранится» — срок вышел или вытеснен новыми, а не
«его нет». Уборка ленивая — при создании и при открытии. Запись не роняет
вызывающего: сбой — строкой журнала и пустой ключ (хвост останется текстом);
промах при открытии (ключа нет, снимок из другого чата) — строкой журнала.
В бэкап снимки не идут (domain/backup/archive), в реплику резерва — тоже
(белый список состояния).
"""
from __future__ import annotations

import json
import logging
import secrets
import string

from awgbot.util import listlink, timeutil

PREFIX = "list_snap:"
TTL_S = 30 * 86400
TOMB_TTL_S = 90 * 86400
PER_CHAT = 20
TOMBS_PER_CHAT = 50
MAX_ROWS = 2000
MAX_BYTES = 256 * 1024
LOG_TAIL_ROWS = 50                 # отрезанных строк на одну строку журнала
LOG_ROW_CHARS = 200
# хвост снимка после «… и ещё N — »: отрезанное — в журнале; чего источник не дал — нигде здесь
TAIL_IN_LOG = "полный список в журнале бота"
TAIL_LEFT_OUT = "в снимок не вошли"
_ALPHABET = string.ascii_lowercase + string.digits
WHERE = f"substr(key, 1, {len(PREFIX)}) = '{PREFIX}'"   # все снимки; не LIKE: «_» там — подстановка (и бэкапу)

log = logging.getLogger("awgbot.db")


def new_key() -> str:
    """Ключ снимка: 10 знаков [a-z0-9] из secrets (≈ 52 бита)."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(10))


def snap_param(db, bot_username: str, chat: int, role: str, title: str, rows, back: str = "",
               new: bool = False, n: int | None = None, missing: int = 0, tail_note: str = "") -> str:
    """Параметр /start снимка (`list-snap-<ключ>`) или пусто. Здесь, а не в слое бота:
    зовут и сервисы домена (Т-Ж4), и обработчики (bot/lists/snapshot.snap_param — в потоке).
    Без имени бота снимок не пишется: ссылки в тексте всё равно не будет, а запись
    занимала бы место в двадцати снимках чата. tail_note — чем кончить строку
    «… и ещё N — …», когда остаток лежит не в журнале этого бота."""
    if not bot_username:
        return ""
    key = db.list_snap_keep(int(chat), role, title, list(rows), back, new, n, missing, tail_note)
    return listlink.build("snap", key=key) if key else ""


class ListSnapMixin:

    def _snaps(self) -> list[tuple[str, dict]]:
        out = []
        for row in self._connection().execute(f"SELECT key, value FROM server_state WHERE {WHERE}").fetchall():
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
    def _tomb(data: dict, why: str = "") -> str:
        """Отметка на месте снимка: why — "" (срок вышел) или "evicted" (вытеснен
        новыми снимками чата) — экран называет причину."""
        return json.dumps({"chat": int(data.get("chat") or 0), "at": str(data.get("at") or ""), "gone": 1,
                           "why": why})

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
                tomb.append((k, d, ""))
            elif int(d.get("chat") or 0) == int(chat):
                live_mine.append((int(d.get("seq") or 0), k, d))
        live_mine.sort(key=lambda x: x[0])
        while len(live_mine) > PER_CHAT - 1:
            _s, k, d = live_mine.pop(0)
            tomb.append((k, d, "evicted"))
        new_tombs = sum(1 for _k, d, _w in tomb if int(d.get("chat") or 0) == int(chat))
        tombs_mine.sort()
        while tombs_mine and len(tombs_mine) + new_tombs > TOMBS_PER_CHAT:
            drop.append(tombs_mine.pop(0)[1])
        if tomb or drop:
            with self._tx() as c:
                c.executemany("UPDATE server_state SET value = ? WHERE key = ?",
                              [(self._tomb(d, why), PREFIX + k) for k, d, why in tomb])
                c.executemany("DELETE FROM server_state WHERE key = ?", [(PREFIX + k,) for k in drop])
                self._tx_note(self._TX_COUNTERS)

    def list_snap_keep(self, chat: int, role: str, title: str, rows: list[str], back: str = "",
                       new: bool = False, n: int | None = None, missing: int = 0, tail_note: str = "") -> str:
        """Положить снимок списка; вернуть его ключ (пусто — не записан: строка журнала,
        хвост у вызывающего останется текстом). n — сколько элементов на деле (шапка);
        missing — сколько их источник не передал строками (в журнале здесь их нет);
        tail_note — чем кончить строку «… и ещё N — …» вместо текста по умолчанию
        (вызывающий знает, где остальное)."""
        try:
            return self._snap_keep(chat, role, title, rows, back, new, n, max(0, int(missing or 0)),
                                   str(tail_note or ""))
        except Exception as e:                                # noqa: BLE001 — снимок не должен ронять итог
            log.error("lists: снимок «%s» не записан: %s", title, e)
            return ""

    def _snap_keep(self, chat, role, title, rows, back, new, n, missing, tail_note) -> str:
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
        # отрезанное здесь целиком уйдёт в журнал; чего источник не дал, в журнале нет
        note = tail_note or (TAIL_IN_LOG if not missing else TAIL_LEFT_OUT)
        while True:
            if not tail_note and missing and keep < len(rows):
                # обрезали и здесь: часть хвоста в журнале есть, часть источник не дал
                note = f"{missing} {TAIL_LEFT_OUT}, остальные — в журнале бота"
            tail = [f"… и ещё {total - keep} — {note}"] if keep < total else []
            body["rows"] = rows[:keep] + tail
            raw = json.dumps(body, ensure_ascii=False)
            if len(raw.encode()) <= MAX_BYTES:
                break
            if keep <= 1:
                raise ValueError(f"одна строка не влезает в {MAX_BYTES // 1024} КБ")
            keep = max(1, keep - max(1, keep // 10))          # не влезло по объёму — короче на десятую
        self._snap_sweep(snaps, int(chat))
        have = {k for k, _d in snaps}
        key = new_key()
        while key in have:
            key = new_key()
        if keep < len(rows):
            # ключ выбран до строк журнала: по нему снимок сопоставим со ссылкой, когда
            # заголовки одинаковые (две рассылки, два итога «🗺 Подсети»)
            log.info("lists: «%s» (снимок %s, чат %d) — %d строк, в снимок вошли %d, остальные — ниже",
                     title, key, int(chat), len(rows), keep)
            for start in range(keep, len(rows), LOG_TAIL_ROWS):
                cut = [r[:LOG_ROW_CHARS] for r in rows[start:start + LOG_TAIL_ROWS]]
                log.info("lists: снимок %s — не вошли (строки %d–%d из %d): %s", key, start + 1,
                         start + len(cut), len(rows), cut)
        with self._tx() as c:
            c.execute("INSERT INTO server_state (key, value) VALUES (?, ?)", (PREFIX + key, raw))
            self._tx_note(self._TX_COUNTERS)
        return key

    def list_snap_get(self, key: str, chat: int) -> dict | None:
        """Снимок по ключу из этого чата; протухший или вытеснённый —
        {"stale": True, "at": …, "why": ""|"evicted"}; нет или чужой — None (и
        строка журнала: по экрану «больше нет» причину не разобрать)."""
        row = self._connection().execute("SELECT value FROM server_state WHERE key = ?", (PREFIX + str(key),)).fetchone()
        if row is None:
            log.info("lists: снимок %s не найден (чат %d)", key, int(chat))
            return None
        try:
            data = json.loads(row[0] or "{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            log.info("lists: снимок %s — запись не читается (чат %d)", key, int(chat))
            return None
        if int(data.get("chat") or 0) != int(chat):
            log.info("lists: снимок %s — из другого чата, открывал %d", key, int(chat))
            return None                                      # чужой чат — как несуществующий
        if data.get("gone"):
            return {"stale": True, "at": str(data.get("at") or ""), "why": str(data.get("why") or "")}
        if self._snap_age(data) > TTL_S:
            with self._tx() as c:
                c.execute("UPDATE server_state SET value = ? WHERE key = ?", (self._tomb(data), PREFIX + str(key)))
                self._tx_note(self._TX_COUNTERS)
            return {"stale": True, "at": str(data.get("at") or ""), "why": ""}
        return data
