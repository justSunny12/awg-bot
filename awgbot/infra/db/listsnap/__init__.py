"""listsnap — снимки событий для экрана «весь список» (bot/lists, вид snap) и
записи сообщений, которые их показывают.

Итог действия, отчёт или уведомление, чей список не влез, кладут его сюда
целиком готовыми строками HTML, а в тексте оставляют хвост «и ещё N» ссылкой
`list-snap-<ключ>`. Всё — в `server_state` под префиксом `list_snap:` (у
основного бота, у бота основы на резерве и у агента — своя база).

Снимок — `list_snap:<ключ>` (ключ — 10 знаков [a-z0-9] из secrets: не
перебирается, не совпадает со старым после восстановления базы):

    {"v": 2, "chat": …, "role": "admin", "at": iso, "exp": iso, "seq": n, "title": "…",
     "n": N, "rows": […], "back": "<колбэк>", "new": false, "msg": <номер сообщения | 0>}

`exp` — момент смерти: полночь в поясе показа после 30 дней (expiry); `msg` —
сообщение, которое его показывает (0 — не показан или отпущен). Снимок v1
(4.1.0, без `exp` и `msg`) читается как есть: срок выводится из `at` тем же
правилом. `role` и `new` — справочные. Открыть снимок можно только из чата,
где он создан. В чате — не больше PER_CHAT снимков (вытесняется самый ранний
по `seq`), в снимке — не больше MAX_ROWS строк и MAX_BYTES; сверх — последняя
строка «… и ещё N — полный список в журнале бота», отрезанное уходит в журнал
порциями (см. snaps). Протухший или вытесненный — отметка `{"chat", "at",
"gone": 1, "why": ""|"evicted"}` (держится TOMB_TTL_S): ссылка отвечает «список
больше не хранится».

Запись сообщения — `list_snap:m:<чат>:<номер>`, одна на сообщение (msgs):

    {"v": 1, "chat": …, "mid": …, "sent": iso, "kb": "<InlineKeyboardMarkup JSON>" | "",
     "keys": ["<ключ>", …], "html": "<текст>" | "", "note": "<итог>" | "", "upd": iso}

Запись держится по трём причинам: ссылки на снимки (`keys`, тогда `html` —
текст, по которому правится ссылка), кнопка «Скрыть» в клавиатуре (живёт
HIDE_KEEP_S — такт после полуночи её снимает), итог первой строкой живого
меню (`note`). Ни одной — записи нет. Двоеточие в ключе отличает запись от
снимка: ключ снимка двоеточия не содержит, listlink такого не разберёт.
Ведёт записи посредник запросов сессии бота (bot/lists/msgs), правит
сообщения он же: ссылка умершего снимка — текстом, «Скрыть» старше суток —
прочь. Уборка снимков — при создании, при открытии и тактом после полуночи
(runtime/scheduler/listsnap). Запись не роняет вызывающего: сбой — строкой
журнала. В бэкап ничего из этого не идёт (domain/backup/archive, по WHERE), в
реплику резерва — тоже (белый список состояния)."""
from __future__ import annotations

import logging
import secrets
import string

from awgbot.util import listlink

from .expiry import HIDE_KEEP_S, TOMB_TTL_S, TTL_S, expires_at, hide_due, is_due   # noqa: F401 — часть API пакета
from .msgs import _MsgsPart, kb_has_action, kb_has_hide
from .snaps import _SnapsPart

PREFIX = "list_snap:"
MSG_PREFIX = PREFIX + "m:"
PER_CHAT = 50
TOMBS_PER_CHAT = 50
MAX_ROWS = 2000
MAX_BYTES = 256 * 1024
LOG_TAIL_ROWS = 50                 # отрезанных строк на одну строку журнала
LOG_ROW_CHARS = 200
# хвост снимка после «… и ещё N — »: отрезанное — в журнале; чего источник не дал — нигде здесь
TAIL_IN_LOG = "полный список в журнале бота"
TAIL_LEFT_OUT = "в снимок не вошли"
_ALPHABET = string.ascii_lowercase + string.digits
WHERE = f"substr(key, 1, {len(PREFIX)}) = '{PREFIX}'"   # снимки, отметки и записи; не LIKE: «_» там — подстановка (и бэкапу)

log = logging.getLogger("awgbot.db")


def new_key() -> str:
    """Ключ снимка: 10 знаков [a-z0-9] из secrets (≈ 52 бита)."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(10))


def snap_param(db, bot_username: str, chat: int, role: str, title: str, rows, back: str = "",
               new: bool = False, n: int | None = None, missing: int = 0, tail_note: str = "") -> str:
    """Параметр /start снимка (`list-snap-<ключ>`) или пусто. Здесь, а не в слое бота:
    зовут и сервисы домена (Т-Ж4), и обработчики (bot/lists/snapshot.snap_param — в потоке).
    Без имени бота снимок не пишется: ссылки в тексте всё равно не будет, а запись
    занимала бы место среди снимков чата. tail_note — чем кончить строку
    «… и ещё N — …», когда остаток лежит не в журнале этого бота."""
    if not bot_username:
        return ""
    key = db.list_snap_keep(int(chat), role, title, list(rows), back, new, n, missing, tail_note)
    return listlink.build("snap", key=key) if key else ""


class ListSnapMixin(_SnapsPart, _MsgsPart):
    """Снимки (snaps) и записи сообщений (msgs) — одна примесь базы."""


__all__ = ["PREFIX", "MSG_PREFIX", "TTL_S", "TOMB_TTL_S", "PER_CHAT", "TOMBS_PER_CHAT", "MAX_ROWS", "MAX_BYTES",
           "LOG_TAIL_ROWS", "LOG_ROW_CHARS", "TAIL_IN_LOG", "TAIL_LEFT_OUT", "HIDE_KEEP_S", "WHERE",
           "new_key", "snap_param", "ListSnapMixin", "expires_at", "is_due", "hide_due", "kb_has_hide", "kb_has_action"]
