"""Сторож эквивалентности журнала данных резерва (режим прогона AWG_JRN_EQUIV=1).

Гарантия журнала — «ни одна запись приложения не идёт мимо него»: следующий,
применивший события держателя к своей копии, получает те же решения, маски и
данные хоста основы, что у держателя. Сторож проверяет это на каждом
интеграционном тесте: на начале — `capture = 1` и копия базы, после теста —
события живой базы применяются к копии (`rsvjrn.apply`) и таблицы этих
классов сравниваются с живыми. История — только в гостевом режиме (от основы
она не едет).

Расхождение значит: где-то в коде запись прошла мимо триггеров (сырое
соединение, `INSERT OR REPLACE` по уникальному ключу, не первичному, и т. п.)
— следующий в отъезде решал бы по базе, которой у держателя нет. Законные
исключения — тесты, которые сами пишут сырым соединением, подменяют файл
базы восстановлением или сливают базы при возврате прежним путём; свои
такие тесты помечаются `pytest.mark.jrn_raw` с причиной.
"""
from __future__ import annotations

import os
import sqlite3

ENV = "AWG_JRN_EQUIV"


def enabled() -> bool:
    return os.environ.get(ENV, "") == "1"


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def events_after(conn: sqlite3.Connection, after: int):
    """События базы с номером больше after — по порядку (для сверок и тестов; рабочий код
    берёт их через infra/db/jrn `jrn_events_after`, пачками и по эпохе)."""
    from awgbot.util.rsvjrn.apply import Event
    return [Event.from_row(r) for r in conn.execute(
        "SELECT i, e, t, op, k, c, o, at FROM main.jrn_events WHERE i > ? ORDER BY i", (int(after),))]


def start(db, copy_path: str) -> None:
    """Включить захват на живой базе и снять её копию (до записей теста)."""
    conn = db._connection()
    conn.execute("UPDATE jrn_meta SET capture = 1 WHERE id = 1")
    conn.commit()
    dst = sqlite3.connect(copy_path)
    try:
        conn.backup(dst)
    finally:
        dst.close()


def _compared(conn, guest: bool) -> dict[str, tuple[list[str], list[str]]]:
    """Таблица → (ключ, сравниваемые колонки) по реестру и живой схеме."""
    from awgbot.util.rsvjrn import classes, triggers
    out: dict[str, tuple[list[str], list[str]]] = {}
    for table, spec in triggers.schema_of(conn).items():
        if table not in classes.TABLES or table == "server_state":
            continue
        cols = [c for c, _ in spec]
        keys = [c for c, pk in sorted(spec, key=lambda x: x[1]) if pk]
        if classes.history_captured(table, guest):
            # история — дописка: номер строки у получателя свой, сравниваем содержимое
            out[table] = ([], [c for c in cols if c not in keys])
            continue
        cap = classes.captured_columns(table, cols, guest)
        if cap:
            out[table] = (keys, list(dict.fromkeys(keys + cap)))
    return out


def _rows(conn, table: str, keys: list[str], cols: list[str]) -> list[tuple]:
    sel = ", ".join(_q(c) for c in cols)
    order = ", ".join(_q(c) for c in (keys or cols))
    return [tuple(r) for r in conn.execute(f"SELECT {sel} FROM main.{_q(table)} ORDER BY {order}")]


def _state(conn) -> list[tuple]:
    from awgbot.util.rsvjrn import classes
    marks = ", ".join("?" for _ in classes.STATE_DECISION)
    return [tuple(r) for r in conn.execute(
        f"SELECT key, value FROM main.server_state WHERE key IN ({marks}) ORDER BY key", classes.STATE_DECISION)]


def check(live_path: str, copy_path: str, guest: bool) -> list[str]:
    """Применить события живой базы к копии и вернуть расхождения (пусто — равны)."""
    from awgbot.util.rsvjrn import apply as japply
    live = sqlite3.connect(live_path)
    copy = sqlite3.connect(copy_path, isolation_level=None)
    try:
        copy.execute("PRAGMA foreign_keys = ON")              # как у следующего: соединение DatabaseCore
        res = japply.apply(copy, events_after(live, 0))
        return [f"не встало: {ev.t} {ev.op} {ev.k}: {why}" for ev, why in res.lost] + diff(live, copy, guest)
    finally:
        live.close()
        copy.close()


def diff(live, copy, guest: bool = False) -> list[str]:
    """Расхождения решений, масок, данных хоста (и истории у гостя) двух баз."""
    problems: list[str] = []
    for table, (keys, cols) in _compared(live, guest).items():
        a, b = _rows(live, table, keys, cols), _rows(copy, table, keys, cols)
        if a != b:
            only_live = [r for r in a if r not in b][:3]
            only_copy = [r for r in b if r not in a][:3]
            problems.append(f"{table}: у держателя {len(a)} строк, у следующего {len(b)}; "
                            f"только у держателя {only_live}, только у следующего {only_copy} ({cols})")
    a, b = _state(live), _state(copy)
    if a != b:
        problems.append(f"server_state: у держателя {a}, у следующего {b}")
    return problems
