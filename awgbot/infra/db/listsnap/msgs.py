"""msgs.py — часть пакета listsnap: записи сообщений (`list_snap:m:<чат>:<номер>`).

Одна запись на сообщение; причины держать её — ссылки на снимки (`keys` и
`html`), кнопка «Скрыть» в клавиатуре (`kb`), итог первой строкой живого меню
(`note`). Пишет посредник запросов сессии бота (bot/lists/msgs/watch) после
удачной отправки или правки; читает он же и такт после полуночи. Все методы
синхронные, в потоке базы (call).

Привязка снимка к сообщению — только снимка этого чата, живого и ещё никому
не показанного: админ, вставивший в объявление чужую ссылку `list-snap-…`,
ничего не привяжет. Ушедшая из текста ссылка отпускает снимок (`msg` = 0),
не удаляя его: на нём может стоять открытый экран «весь список». Нажатая
«Скрыть» и удаление сообщения снимают запись и его снимки без отметки.

Клавиатура хранится JSON-строкой InlineKeyboardMarkup; «Скрыть» узнаётся по
колбэку `hd` (bot/callbacks.HideCB), кнопки действия — всё остальное. `cap` —
текст записи — подпись к фото или файлу: править её editMessageCaption."""
from __future__ import annotations

import json
import logging

from awgbot.util import timeutil

from . import expiry

log = logging.getLogger("awgbot.db")

HIDE_CB = "hd"


def _buttons(kb: str | None) -> list[dict]:
    """Кнопки клавиатуры из JSON (пусто или нечитаемо — ни одной)."""
    if not kb:
        return []
    try:
        data = json.loads(kb)
    except ValueError:
        return []
    rows = data.get("inline_keyboard") if isinstance(data, dict) else None
    return [b for row in (rows or []) if isinstance(row, list) for b in row if isinstance(b, dict)]


def kb_has_hide(kb: str | None) -> bool:
    """В клавиатуре есть «Скрыть»."""
    return any(str(b.get("callback_data") or "") == HIDE_CB for b in _buttons(kb))


def kb_has_action(kb: str | None) -> bool:
    """В клавиатуре есть кнопки кроме «Скрыть» (живое меню, «⏱ Продлить» и т. п.)."""
    return any(str(b.get("callback_data") or "") != HIDE_CB for b in _buttons(kb))


def _pkg():
    from awgbot.infra.db import listsnap
    return listsnap


class _MsgsPart:

    @staticmethod
    def _msg_key(chat: int, mid: int) -> str:
        return f"{_pkg().MSG_PREFIX}{int(chat)}:{int(mid)}"

    def _msg_write(self, rec: dict) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO server_state (key, value) VALUES (?, ?)",
                      (self._msg_key(rec["chat"], rec["mid"]), json.dumps(rec, ensure_ascii=False)))
            self._tx_note(self._TX_COUNTERS)

    def _msg_delete(self, chat: int, mid: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM server_state WHERE key = ?", (self._msg_key(chat, mid),))
            self._tx_note(self._TX_COUNTERS)

    def list_msg_get(self, chat: int, mid: int) -> dict | None:
        row = self._connection().execute("SELECT value FROM server_state WHERE key = ?",
                                         (self._msg_key(chat, mid),)).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row[0] or "{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) and data.get("mid") else None

    def list_msg_all(self, chat: int | None = None) -> list[dict]:
        """Записи сообщений — все (такт, снятие на `away`) или одного чата (правка в
        действии): выбор по префиксу ключа в SQL, двоеточие после номера чата не
        даёт -1 совпасть с -100…."""
        p = _pkg()
        head = p.MSG_PREFIX if chat is None else f"{p.MSG_PREFIX}{int(chat)}:"
        out = []
        for row in self._connection().execute(
                f"SELECT value FROM server_state WHERE substr(key, 1, {len(head)}) = ?", (head,)).fetchall():
            try:
                data = json.loads(row[0] or "{}")
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("mid"):
                out.append(data)
        return out

    def list_msg_put(self, chat: int, mid: int, sent: str, html: str, kb: str | None,
                     keys: list[str], note: str | None, cap: bool | None = None) -> bool:
        """Отправка или правка: поля — по новому виду сообщения; kb/note/cap None — не
        менялись. Ушедшие ключи отпущены (msg = 0), новые ключи снимков ЭТОГО чата,
        ещё не показанные, привязаны; причин держать запись нет — записи нет.
        True — привязалась новая ссылка (вызывающий правит чат: вытесненные — сейчас)."""
        chat, mid = int(chat), int(mid)
        rec = self.list_msg_get(chat, mid) or {}
        cap_now = bool(rec.get("cap")) if cap is None else bool(cap)
        old_keys = [str(k) for k in (rec.get("keys") or [])]
        want = []
        for k in keys:
            k = str(k)
            if k and k not in want:
                want.append(k)
        bound = [k for k in old_keys if k in want]
        new_bound = False
        for k in want:
            if k in bound:
                continue
            if self._snap_bind(k, chat, mid):
                bound.append(k)
                new_bound = True
        for k in old_keys:
            if k not in want:
                self._snap_release(k, mid)
        kb_now = rec.get("kb", "") if kb is None else str(kb or "")
        note_now = rec.get("note", "") if note is None else str(note or "")
        if not bound and not kb_has_hide(kb_now) and not note_now:
            if rec:
                self._msg_delete(chat, mid)
            return new_bound
        self._msg_write({"v": 1, "chat": chat, "mid": mid, "sent": str(rec.get("sent") or sent or ""),
                         "kb": kb_now, "keys": bound, "html": str(html or "") if bound else "",
                         "note": note_now, "upd": timeutil.now_iso(), **({"cap": True} if cap_now else {})})
        return new_bound

    def list_msg_set_kb(self, chat: int, mid: int, sent: str, kb: str) -> None:
        """Правка одной клавиатуры (кнопки сняты нажатием, итоги обновлений): ключи и
        текст — прежние; «Скрыть» ушла и других причин нет — записи нет."""
        rec = self.list_msg_get(chat, mid) or {}
        self.list_msg_put(chat, mid, sent, str(rec.get("html") or ""), kb, [str(k) for k in (rec.get("keys") or [])], None)

    def list_msg_forget(self, chat: int, mid: int, drop_snaps: bool) -> list[str]:
        """Сообщения нет («Скрыть», удаление, «not found»): запись прочь; drop_snaps —
        и его снимки, без отметки, иначе снимки отпущены (msg = 0) и живут до срока.
        Возвращает снятые ключи (строка журнала)."""
        p = _pkg()
        rec = self.list_msg_get(chat, mid)
        if rec is None:
            return []
        keys = [str(k) for k in (rec.get("keys") or [])]
        with self._tx() as c:
            c.execute("DELETE FROM server_state WHERE key = ?", (self._msg_key(chat, mid),))
            if drop_snaps and keys:
                c.executemany("DELETE FROM server_state WHERE key = ?", [(p.PREFIX + k,) for k in keys])
            self._tx_note(self._TX_COUNTERS)
        if not drop_snaps:
            for k in keys:
                self._snap_release(k, mid)
        return keys if drop_snaps else []

    def list_msg_final(self, chat: int, mid: int) -> None:
        """Править нечего и не будет (403, прочие 400): запись прочь, отметки остаются."""
        if self.list_msg_get(chat, mid) is not None:
            self._msg_delete(chat, mid)

    def _msg_due(self, rec: dict, now) -> dict | None:
        """Запись с полями `dead` (ключи, чьи ссылки снять) и `hide` (ряд «Скрыть»
        снять) или None — править нечего."""
        dead = [k for k in (rec.get("keys") or []) if self._snap_dead(str(k), now)]
        hide = kb_has_hide(rec.get("kb")) and expiry.hide_due(rec, now)
        return {**rec, "dead": dead, "hide": hide} if dead or hide else None

    def list_msg_due(self, now=None, chat: int | None = None) -> list[dict]:
        """Записи к правке: мёртвые ключи и/или «Скрыть» старше HIDE_KEEP_S."""
        now = now or timeutil.now()
        out = []
        for rec in self.list_msg_all(chat):
            due = self._msg_due(rec, now)
            if due:
                out.append(due)
        return out

    def list_msg_due_one(self, chat: int, mid: int, now=None) -> dict | None:
        """Одна запись к правке (перечитывание под замком чата) или None."""
        rec = self.list_msg_get(chat, mid)
        return self._msg_due(rec, now or timeutil.now()) if rec else None

    def list_msg_drop_note(self, chat: int, mid: int) -> None:
        """Указатель меню ушёл с сообщения мимо гашения (/start, человек удалил сам,
        правка до итога не удалась): итог записи больше не нужен — снять; без других
        причин записи нет."""
        rec = self.list_msg_get(chat, mid)
        if rec and rec.get("note"):
            self.list_msg_put(chat, mid, str(rec.get("sent") or ""), str(rec.get("html") or ""), None,
                              [str(k) for k in (rec.get("keys") or [])], "")

    def list_msg_drop_notes(self, chat: int, mids) -> None:
        for mid in mids:
            self.list_msg_drop_note(chat, int(mid))

    def list_msg_sweep(self, now=None) -> int:
        """Записи, не менявшиеся дольше TOMB_TTL_S (меню ушло мимо гашения и учёта), —
        прочь; возвращает число снятых."""
        p = _pkg()
        now = now or timeutil.now()
        gone = []
        for rec in self.list_msg_all():
            try:
                age = timeutil.elapsed(timeutil.parse_iso(str(rec.get("upd") or rec.get("sent") or "")), now)
            except ValueError:
                age = float("inf")
            if age > p.TOMB_TTL_S:
                gone.append(rec)
        if gone:
            with self._tx() as c:
                c.executemany("DELETE FROM server_state WHERE key = ?",
                              [(self._msg_key(rec["chat"], rec["mid"]),) for rec in gone])
                self._tx_note(self._TX_COUNTERS)
        return len(gone)

    def list_msg_drop_live(self) -> int:
        """Право отвечать ушло резерву: записи сообщений с кнопками действия — прочь
        (бот на резерве правит такие сообщения сам); снимки остаются до срока."""
        gone = [rec for rec in self.list_msg_all() if kb_has_action(rec.get("kb"))]
        if gone:
            with self._tx() as c:
                c.executemany("DELETE FROM server_state WHERE key = ?",
                              [(self._msg_key(rec["chat"], rec["mid"]),) for rec in gone])
                self._tx_note(self._TX_COUNTERS)
        return len(gone)
