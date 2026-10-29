"""snaps.py — часть пакета listsnap: снимки и отметки в server_state.

Запись (`list_snap_keep`): тело v2 с `exp` и `msg` = 0; перед записью — уборка
(`_snap_sweep`: протухшие любого чата — в отметки, в чате — живых не больше
PER_CHAT − 1, вытесняется самый ранний по `seq`; отметок — не больше
TOMBS_PER_CHAT, старые прочь). Пределы MAX_ROWS и MAX_BYTES: сверх — последняя
строка «… и ещё N — …», отрезанное целиком уходит в журнал порциями по
LOG_TAIL_ROWS строк (строка — до LOG_ROW_CHARS знаков), каждая строка журнала —
с ключом снимка и чатом. Когда источник сам отдал не все (`missing`), в
журнале их нет, и хвост говорит это («… и ещё N — в снимок не вошли»);
обрезало и хранилище — обе части. Текст вызывающего (`tail_note`) заменяет оба
варианта. N в шапке — настоящее число элементов (`n`). Одна строка больше
MAX_BYTES — снимок не пишется (сбой записи, хвост у вызывающего остаётся
текстом).

Чтение (`list_snap_get`): протухший — по `exp` (экран и такт говорят одно и то
же), чужой чат — как несуществующий. Такт (`list_snap_expire_due`): снимки с
наступившим сроком — в отметки, старые отметки — прочь; возвращает, чьи
сообщения теперь к правке."""
from __future__ import annotations

import json
import logging

from awgbot.util import timeutil

from . import expiry

log = logging.getLogger("awgbot.db")


def _pkg():
    from awgbot.infra.db import listsnap
    return listsnap


class _SnapsPart:

    def _snaps(self) -> list[tuple[str, dict]]:
        """Снимки и отметки (записи сообщений — `m:` — не здесь)."""
        p = _pkg()
        out = []
        for row in self._connection().execute(
                f"SELECT key, value FROM server_state WHERE {p.WHERE} AND instr(substr(key, {len(p.PREFIX) + 1}), ':') = 0"
        ).fetchall():
            key = str(row[0])[len(p.PREFIX):]
            try:
                data = json.loads(row[1] or "{}")
            except ValueError:
                data = {}
            out.append((key, data if isinstance(data, dict) else {}))
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
        p = _pkg()
        now = timeutil.now()
        tomb, drop = [], []
        live_mine, tombs_mine = [], []
        for k, d in snaps:
            if d.get("gone"):
                if self._snap_age(d) > p.TOMB_TTL_S:
                    drop.append(k)
                elif int(d.get("chat") or 0) == int(chat):
                    tombs_mine.append((str(d.get("at") or ""), k))
                continue
            if expiry.is_due(d, now):
                tomb.append((k, d, ""))
            elif int(d.get("chat") or 0) == int(chat):
                live_mine.append((int(d.get("seq") or 0), k, d))
        live_mine.sort(key=lambda x: x[0])
        while len(live_mine) > p.PER_CHAT - 1:
            _s, k, d = live_mine.pop(0)
            tomb.append((k, d, "evicted"))
        new_tombs = sum(1 for _k, d, _w in tomb if int(d.get("chat") or 0) == int(chat))
        tombs_mine.sort()
        while tombs_mine and len(tombs_mine) + new_tombs > p.TOMBS_PER_CHAT:
            drop.append(tombs_mine.pop(0)[1])
        self._snap_apply(tomb, drop)

    def _snap_apply(self, tomb: list[tuple[str, dict, str]], drop: list[str]) -> None:
        p = _pkg()
        if tomb or drop:
            with self._tx() as c:
                c.executemany("UPDATE server_state SET value = ? WHERE key = ?",
                              [(self._tomb(d, why), p.PREFIX + k) for k, d, why in tomb])
                c.executemany("DELETE FROM server_state WHERE key = ?", [(p.PREFIX + k,) for k in drop])
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
        p = _pkg()
        rows = [str(r) for r in rows]
        snaps = self._snaps()
        seq = max((int(d.get("seq") or 0) for _k, d in snaps), default=0) + 1
        at = timeutil.now_iso()
        body = {"v": 2, "chat": int(chat), "role": str(role), "at": at, "exp": expiry.exp_iso(at), "seq": seq,
                "title": str(title), "n": int(n if n is not None else len(rows)), "rows": rows,
                "back": str(back or ""), "new": bool(new), "msg": 0}
        total = len(rows) + missing                          # источник мог отдать не все
        keep = min(len(rows), p.MAX_ROWS)
        if keep < total and keep == p.MAX_ROWS:
            keep -= 1                                         # место под строку «и ещё N»
        # отрезанное здесь целиком уйдёт в журнал; чего источник не дал, в журнале нет
        note = tail_note or (p.TAIL_IN_LOG if not missing else p.TAIL_LEFT_OUT)
        while True:
            if not tail_note and missing and keep < len(rows):
                # обрезали и здесь: часть хвоста в журнале есть, часть источник не дал
                note = f"{missing} {p.TAIL_LEFT_OUT}, остальные — в журнале бота"
            tail = [f"… и ещё {total - keep} — {note}"] if keep < total else []
            body["rows"] = rows[:keep] + tail
            raw = json.dumps(body, ensure_ascii=False)
            if len(raw.encode()) <= p.MAX_BYTES:
                break
            if keep <= 1:
                raise ValueError(f"одна строка не влезает в {p.MAX_BYTES // 1024} КБ")
            keep = max(1, keep - max(1, keep // 10))          # не влезло по объёму — короче на десятую
        self._snap_sweep(snaps, int(chat))
        have = {k for k, _d in snaps}
        key = p.new_key()
        while key in have:
            key = p.new_key()
        if keep < len(rows):
            # ключ выбран до строк журнала: по нему снимок сопоставим со ссылкой, когда
            # заголовки одинаковые (две рассылки, два итога «🗺 Подсети»)
            log.info("lists: «%s» (снимок %s, чат %d) — %d строк, в снимок вошли %d, остальные — ниже",
                     title, key, int(chat), len(rows), keep)
            for start in range(keep, len(rows), p.LOG_TAIL_ROWS):
                cut = [r[:p.LOG_ROW_CHARS] for r in rows[start:start + p.LOG_TAIL_ROWS]]
                log.info("lists: снимок %s — не вошли (строки %d–%d из %d): %s", key, start + 1,
                         start + len(cut), len(rows), cut)
        with self._tx() as c:
            c.execute("INSERT INTO server_state (key, value) VALUES (?, ?)", (p.PREFIX + key, raw))
            self._tx_note(self._TX_COUNTERS)
        return key

    def _snap_raw(self, key: str) -> dict | None:
        """Тело снимка или отметки по ключу; нет или нечитаемо — None."""
        p = _pkg()
        row = self._connection().execute("SELECT value FROM server_state WHERE key = ?",
                                         (p.PREFIX + str(key),)).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row[0] or "{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def list_snap_get(self, key: str, chat: int) -> dict | None:
        """Снимок по ключу из этого чата; протухший или вытеснённый —
        {"stale": True, "at": …, "why": ""|"evicted"}; нет или чужой — None (и
        строка журнала: по экрану «больше нет» причину не разобрать)."""
        p = _pkg()
        if ":" in str(key):
            log.info("lists: ключ %s — не снимок (чат %d)", key, int(chat))
            return None
        data = self._snap_raw(key)
        if data is None:
            row = self._connection().execute("SELECT 1 FROM server_state WHERE key = ?",
                                             (p.PREFIX + str(key),)).fetchone()
            log.info("lists: снимок %s %s (чат %d)", key, "— запись не читается" if row else "не найден", int(chat))
            return None
        if int(data.get("chat") or 0) != int(chat):
            log.info("lists: снимок %s — из другого чата, открывал %d", key, int(chat))
            return None                                      # чужой чат — как несуществующий
        if data.get("gone"):
            return {"stale": True, "at": str(data.get("at") or ""), "why": str(data.get("why") or "")}
        if expiry.is_due(data):
            self._snap_apply([(str(key), data, "")], [])
            return {"stale": True, "at": str(data.get("at") or ""), "why": ""}
        return data

    def list_snap_expire_due(self, now=None) -> list[tuple[str, int]]:
        """Такт после полуночи: снимки с наступившим сроком — в отметки (любой чат),
        отметки старше TOMB_TTL_S — прочь. Возвращает (ключ, чат) сжатых снимков —
        их сообщения теперь к правке."""
        p = _pkg()
        now = now or timeutil.now()
        tomb, drop = [], []
        for k, d in self._snaps():
            if d.get("gone"):
                if self._snap_age(d) > p.TOMB_TTL_S:
                    drop.append(k)
            elif expiry.is_due(d, now):
                tomb.append((k, d, ""))
        self._snap_apply(tomb, drop)
        return [(k, int(d.get("chat") or 0)) for k, d, _w in tomb]

    def _snap_bind(self, key: str, chat: int, mid: int) -> bool:
        """Привязать снимок к сообщению: только свой чат, живой и ещё не показанный.
        True — привязан."""
        p = _pkg()
        data = self._snap_raw(key)
        if (not data or data.get("gone") or int(data.get("chat") or 0) != int(chat)
                or int(data.get("msg") or 0) not in (0, int(mid)) or expiry.is_due(data)):
            return False
        if int(data.get("msg") or 0) == int(mid):
            return True                                   # уже этого сообщения (запись терялась)
        data["msg"] = int(mid)
        with self._tx() as c:
            c.execute("UPDATE server_state SET value = ? WHERE key = ?",
                      (json.dumps(data, ensure_ascii=False), p.PREFIX + str(key)))
            self._tx_note(self._TX_COUNTERS)
        return True

    def _snap_release(self, key: str, mid: int) -> None:
        """Отпустить снимок (ссылки в сообщении больше нет): msg = 0, снимок живёт до срока."""
        p = _pkg()
        data = self._snap_raw(key)
        if not data or data.get("gone") or int(data.get("msg") or 0) != int(mid):
            return
        data["msg"] = 0
        with self._tx() as c:
            c.execute("UPDATE server_state SET value = ? WHERE key = ?",
                      (json.dumps(data, ensure_ascii=False), p.PREFIX + str(key)))
            self._tx_note(self._TX_COUNTERS)

    def _snap_dead(self, key: str, now=None) -> bool:
        """Ключ записи мёртв: снимка нет, он отметка или срок наступил."""
        data = self._snap_raw(key)
        return not data or bool(data.get("gone")) or expiry.is_due(data, now)
