"""Снимки событий для экрана «весь список» (infra/db/listsnap) и их вид snap
(bot/lists/kinds/snap) на настоящей базе: запись и чтение, срок 30 дней на
замороженных часах, вытеснение 21-го снимка чата по порядковому номеру,
отметки «больше не хранится» и их уборка, пределы 2000 строк и 256 КБ со
строкой «полный список в журнале бота», сбой записи, чужой чат; шапка,
листание и «⬅️ Назад» / «⬅️ В меню» у каждой роли; пользователь открывает
снимок своего итога «Свои сайты»; в бэкап и в пакет резерва снимки не идут.

Цена ошибки: снимок, открытый из чужого чата, — список адресов, профилей и
отказов чужого человека; снимок, который не протухает, — база растёт от
каждого итога; вытеснение «по секунде» — в пачке итогов одной секунды уходит
случайный, а не самый ранний; без отметки протухший снимок отвечает «списка
нет», будто ссылку подделали; снимок на мегабайты — запись в server_state
на каждом итоге и экран, который не собрать; сбой записи, уронивший итог, —
человек не узнаёт, что вообще добавилось; «⬅️ В меню» гостя на главную
пользователя — кнопка, которую гость нажать не может; снимки в бэкапе —
следы интерфейса чужого чата после восстановления на другом сервере.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import sqlite3

import pytest

from awgbot.bot import lists
from awgbot.bot.callbacks import FriendCB, GwCB, Menu, RoutingCB
from awgbot.core import config
from awgbot.infra.db import listsnap
from awgbot.util import timeutil

pytestmark = pytest.mark.integration

CHAT, OTHER = 5550001, 5550002
T0 = _dt.datetime(2026, 9, 15, 12, 0, 0, tzinfo=config.TZ)
DAY = 86400
_KEY = re.compile(r"[a-z0-9]{10}")


class _Clock:
    """Замороженные часы: timeutil.now() — t; шаг — advance(секунды)."""

    def __init__(self, monkeypatch, start: _dt.datetime = T0):
        self.t = start
        monkeypatch.setattr(timeutil, "now", lambda: self.t)

    def advance(self, seconds: float) -> None:
        self.t = self.t + _dt.timedelta(seconds=seconds)


@pytest.fixture()
def clock(monkeypatch):
    return _Clock(monkeypatch)


def _rows(n: int, tag: str = "r") -> list[str]:
    return [f"<code>{tag}{i:04d}.example</code> — не похоже на адрес" for i in range(n)]


def _raw(db, key: str) -> dict | None:
    row = db._connection().execute("SELECT value FROM server_state WHERE key = ?",
                                   (listsnap.PREFIX + key,)).fetchone()
    return None if row is None else json.loads(row[0])


def _snap_keys(db) -> list[str]:
    return [r[0] for r in db._connection().execute(
        "SELECT key FROM server_state WHERE substr(key, 1, 10) = 'list_snap:'").fetchall()]


def _buttons(markup) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


class _Svc:
    """Минимум, который нужен виду snap: база."""

    def __init__(self, db):
        self.db = db


# ── запись и чтение ─────────────────────────────────────────────────────────

def test_a_snapshot_is_read_back_whole_from_its_chat(db, clock):
    """Ключ — 10 знаков [a-z0-9] (влезает в ссылку list-snap-<ключ>), снимок
    возвращает ровно то, что положил производитель: заголовок, строки HTML,
    колбэк источника, метку «из уведомления» и число элементов."""
    rows = _rows(7)
    key = db.list_snap_keep(CHAT, "admin", "⚠️ Не добавлено", rows, RoutingCB(action="sites", ref=3).pack(), True, 7)
    assert _KEY.fullmatch(key), f"ключ не по грамматике ссылки: {key!r}"
    got = db.list_snap_get(key, CHAT)
    assert got is not None and not got.get("stale"), got
    assert got["rows"] == rows, "строки снимка — не те, что положил источник"
    assert (got["title"], got["back"], got["new"], got["n"], got["role"], got["chat"]) == \
        ("⚠️ Не добавлено", RoutingCB(action="sites", ref=3).pack(), True, 7, "admin", CHAT), got
    assert timeutil.parse_iso(got["at"]) == T0, "время события — не время записи"


def test_without_n_the_number_is_the_number_of_rows(db, clock):
    key = db.list_snap_keep(CHAT, "admin", "x", _rows(3))
    assert db.list_snap_get(key, CHAT)["n"] == 3


def test_two_snapshots_get_different_keys(db, clock):
    a = db.list_snap_keep(CHAT, "admin", "x", _rows(1))
    b = db.list_snap_keep(CHAT, "admin", "x", _rows(1))
    assert a != b and db.list_snap_get(a, CHAT) and db.list_snap_get(b, CHAT)


def test_a_colliding_key_is_drawn_again_and_does_not_overwrite(db, clock, monkeypatch):
    """Совпавший ключ (52 бита — редко, но бывает) не перезаписывает чужой снимок."""
    seq = iter(["aaaaaaaaaa", "aaaaaaaaaa", "bbbbbbbbbb"])
    monkeypatch.setattr(listsnap, "new_key", lambda: next(seq))
    first = db.list_snap_keep(CHAT, "admin", "первый", _rows(1))
    second = db.list_snap_keep(CHAT, "admin", "второй", _rows(1))
    assert (first, second) == ("aaaaaaaaaa", "bbbbbbbbbb"), (first, second)
    assert db.list_snap_get(first, CHAT)["title"] == "первый", "совпавший ключ перезаписал прежний снимок"


def test_a_foreign_chat_sees_no_snapshot_at_all(db, clock):
    """Чужой чат — как несуществующий ключ: ни строк, ни признака «протух»."""
    key = db.list_snap_keep(CHAT, "admin", "x", _rows(2))
    assert db.list_snap_get(key, OTHER) is None, "снимок открылся из чужого чата"
    assert db.list_snap_get("zzzzzzzzzz", CHAT) is None


def test_a_foreign_chat_does_not_learn_that_a_stale_snapshot_existed(db, clock):
    key = db.list_snap_keep(CHAT, "admin", "x", _rows(2))
    clock.advance(31 * DAY)
    assert db.list_snap_get(key, OTHER) is None, "чужому чату сказали «больше не хранится» — ключ подтверждён"
    assert db.list_snap_get(key, CHAT) == {"stale": True, "at": timeutil.to_iso(T0), "why": ""}


# ── срок 30 дней ────────────────────────────────────────────────────────────

def test_a_snapshot_lives_thirty_days_and_then_answers_stale(db, clock):
    key = db.list_snap_keep(CHAT, "admin", "x", _rows(2))
    clock.advance(30 * DAY - 60)
    assert db.list_snap_get(key, CHAT)["rows"] == _rows(2), "снимок протух раньше 30 дней"
    clock.advance(120)
    got = db.list_snap_get(key, CHAT)
    assert got == {"stale": True, "at": timeutil.to_iso(T0), "why": ""}, got
    raw = _raw(db, key)
    assert raw == {"chat": CHAT, "at": timeutil.to_iso(T0), "gone": 1, "why": ""}, \
        f"протухший снимок не сжат в отметку — строки лежат в базе дальше: {raw}"


def test_expired_snapshots_of_any_chat_become_marks_on_the_next_write(db, clock):
    """Уборка ленивая — при создании: протухшие снимки всех чатов сжимаются в отметки."""
    old = db.list_snap_keep(OTHER, "admin", "x", _rows(50))
    clock.advance(31 * DAY)
    db.list_snap_keep(CHAT, "admin", "y", _rows(1))
    assert _raw(db, old) == {"chat": OTHER, "at": timeutil.to_iso(T0), "gone": 1, "why": ""}, _raw(db, old)


# ── 20 на чат: вытеснение по порядковому номеру ─────────────────────────────

def test_the_21st_snapshot_of_a_chat_evicts_the_earliest_even_in_the_same_second(db, clock):
    """Двадцать один итог за одну секунду: уходит первый по порядку, а не случайный
    из одной секунды; соседний чат не трогается."""
    neighbour = db.list_snap_keep(OTHER, "admin", "сосед", _rows(1))
    keys = [db.list_snap_keep(CHAT, "admin", f"№{n}", _rows(1)) for n in range(21)]
    assert db.list_snap_get(keys[0], CHAT) == {"stale": True, "at": timeutil.to_iso(T0), "why": "evicted"}, \
        "21-й снимок не вытеснил самый ранний"
    alive = [k for k in keys[1:] if not (db.list_snap_get(k, CHAT) or {}).get("stale")]
    assert alive == keys[1:], "вытеснен не самый ранний по порядку"
    assert db.list_snap_get(neighbour, OTHER)["title"] == "сосед", "вытеснение задело чужой чат"


def test_twenty_snapshots_of_a_chat_all_stay(db, clock):
    keys = [db.list_snap_keep(CHAT, "admin", f"№{n}", _rows(1)) for n in range(20)]
    assert all(not db.list_snap_get(k, CHAT).get("stale") for k in keys), "вытеснен снимок при двадцати"


def test_marks_over_fifty_per_chat_are_dropped_oldest_first(db, clock):
    """71 итог в чате: 20 живых и 50 отметок; отметка самого первого — прочь (ссылка
    на него отвечает «списка нет»), второго — ещё держится."""
    keys = []
    for n in range(71):
        keys.append(db.list_snap_keep(CHAT, "admin", f"№{n}", _rows(1)))
        clock.advance(1)
    states = [db.list_snap_get(k, CHAT) for k in keys]
    assert states[0] is None, f"отметок в чате больше 50: первая держится ({states[0]})"
    assert all(s and s.get("stale") for s in states[1:51]), "отметка из последних пятидесяти потеряна"
    assert all(s and not s.get("stale") for s in states[51:]), "живых не двадцать последних"
    assert len(_snap_keys(db)) == 70, f"в базе {len(_snap_keys(db))} записей снимков, ждали 20 + 50"


def test_marks_older_than_ninety_days_are_dropped(db, clock):
    key = db.list_snap_keep(CHAT, "admin", "x", _rows(1))
    clock.advance(31 * DAY)
    assert db.list_snap_get(key, CHAT)["stale"]
    clock.advance(58 * DAY)                                 # 89 дней от события
    db.list_snap_keep(OTHER, "admin", "y", _rows(1))
    assert db.list_snap_get(key, CHAT) == {"stale": True, "at": timeutil.to_iso(T0), "why": ""}, "отметка ушла раньше 90 дней"
    clock.advance(2 * DAY)                                  # 91 день
    db.list_snap_keep(OTHER, "admin", "z", _rows(1))
    assert db.list_snap_get(key, CHAT) is None and _raw(db, key) is None, "отметка старше 90 дней осталась в базе"


# ── пределы 2000 строк и 256 КБ ─────────────────────────────────────────────

@pytest.mark.parametrize("n, given, kept, tail", [(2005, 2000, 1999, "6 — 5 в снимок не вошли, остальные — в журнале бота"),
                                                   (600, 500, 500, "100 — в снимок не вошли")])
def test_a_source_that_gave_fewer_rows_than_n_gets_an_honest_left_out_line(db, clock, n, given, kept, tail):
    """Источник отдал не все элементы (missing): последней строкой — сколько
    не вошло, без обещания журнала (чего не дали, в журнале здесь нет); при
    полных 2000 строках место под неё — за счёт последней, и тогда хвост
    различает: одна строка отрезана здесь (она в журнале), пять не дали."""
    src = _rows(given)
    got = db.list_snap_get(db.list_snap_keep(CHAT, "admin", "t", src, n=n, missing=n - given), CHAT)
    assert got["rows"][:-1] == src[:kept] and len(got["rows"]) == kept + 1, len(got["rows"])
    assert got["rows"][-1] == f"… и ещё {tail}", got["rows"][-1]
    assert got["n"] == n


def test_the_caller_can_say_where_the_rest_is(db, clock):
    """tail_note — текст хвоста от вызывающего: у Т-Ж4 после аварии остаток
    спорных правок лежит в журнале бота на резерве, и строка снимка говорит это."""
    key = db.list_snap_keep(CHAT, "admin", "t", _rows(5), n=12, missing=7, tail_note="в журнале бота на «Beta»")
    assert db.list_snap_get(key, CHAT)["rows"][-1] == "… и ещё 7 — в журнале бота на «Beta»"


def test_n_above_the_rows_without_missing_is_not_a_cut(db, clock):
    """Строк меньше n потому, что источник свернул часть в одну (гости рассылки), —
    это не обрезка: строки «в журнале бота» нет, в шапке — n."""
    got = db.list_snap_get(db.list_snap_keep(CHAT, "admin", "t", _rows(31), n=32), CHAT)
    assert got["rows"] == _rows(31) and got["n"] == 32


def test_exactly_two_thousand_rows_fit_without_a_tail(db, clock):
    key = db.list_snap_keep(CHAT, "admin", "x", _rows(2000))
    rows = db.list_snap_get(key, CHAT)["rows"]
    assert rows == _rows(2000), "2000 строк обрезаны"


def test_over_two_thousand_rows_end_with_the_journal_line_and_the_whole_cut_goes_to_the_log(db, clock, caplog):
    """Строка «полный список в журнале бота» — правда: отрезанное уходит в журнал
    целиком, порциями по LOG_TAIL_ROWS, каждая строка журнала — с ключом снимка
    (две рассылки с одним заголовком различимы) и чатом."""
    src = _rows(2500)
    with caplog.at_level(logging.INFO, logger="awgbot.db"):
        key = db.list_snap_keep(CHAT, "admin", "⚠️ Не добавлено", src)
    got = db.list_snap_get(key, CHAT)
    assert len(got["rows"]) == 2000, f"в снимке {len(got['rows'])} строк"
    assert got["rows"][:1999] == src[:1999], "в снимок вошли не первые строки"
    assert got["rows"][-1] == "… и ещё 501 — полный список в журнале бота", got["rows"][-1]
    assert got["n"] == 2500, "в шапке — число строк снимка, а не настоящее число элементов"
    lines = [r.getMessage() for r in caplog.records if "lists:" in r.getMessage()]
    log = "\n".join(lines)
    assert f"«⚠️ Не добавлено» (снимок {key}, чат {CHAT}) — 2500 строк, в снимок вошли 1999" in log, log[:300]
    assert "r1999.example" in log, "первая отрезанная строка не попала в журнал"
    assert "r0000.example" not in log and "r1998.example" not in log, \
        "в журнал ушли строки, которые и так в снимке"
    assert "r2049.example" in log and "r2499.example" in log, "в журнал ушли не все отрезанные строки"
    portions = [ln for ln in lines if ln.startswith(f"lists: снимок {key} — не вошли")]
    assert len(portions) == -(-501 // listsnap.LOG_TAIL_ROWS), f"порций {len(portions)}"
    assert all(len(ln) < 20000 for ln in portions), "строка журнала длиннее, чем journald держит"


def test_the_log_line_of_a_cut_row_is_limited_in_length(db, clock, caplog):
    src = ["x"] * 1999 + ["Я" * 1000, "Ю" * 1000]
    with caplog.at_level(logging.INFO, logger="awgbot.db"):
        db.list_snap_keep(CHAT, "admin", "t", src)
    log = "\n".join(r.getMessage() for r in caplog.records if "lists:" in r.getMessage())
    assert "Я" * 200 in log and "Я" * 201 not in log, "строка журнала не обрезана до 200 знаков"


def test_a_snapshot_never_grows_over_256_kb_and_says_how_many_are_left_out(db, clock):
    src = [f"<code>{i:04d}</code> " + "ж" * 500 for i in range(400)]      # ≈ 400 КБ в UTF-8
    key = db.list_snap_keep(CHAT, "admin", "x", src)
    stored = db._connection().execute("SELECT value FROM server_state WHERE key = ?",
                                      (listsnap.PREFIX + key,)).fetchone()[0]
    assert len(stored.encode()) <= 256 * 1024, f"снимок {len(stored.encode())} байт"
    rows = db.list_snap_get(key, CHAT)["rows"]
    m = re.fullmatch(r"… и ещё (\d+) — полный список в журнале бота", rows[-1])
    assert m, f"нет строки о журнале: {rows[-1][:80]}"
    kept = rows[:-1]
    assert kept == src[:len(kept)] and len(kept) + int(m.group(1)) == 400, \
        f"вошло {len(kept)}, «и ещё {m.group(1)}» — в сумме не 400"
    assert len(kept) > 200, f"обрезано слишком грубо: вошло {len(kept)} из 400"


# ── сбой записи ─────────────────────────────────────────────────────────────

def test_a_failed_write_returns_an_empty_key_and_a_log_line_instead_of_raising(db, clock, monkeypatch, caplog):
    """База заперта на записи — итог человека всё равно приходит (хвост — текстом)."""
    def locked():
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(db, "_tx", locked)
    with caplog.at_level(logging.ERROR, logger="awgbot.db"):
        key = db.list_snap_keep(CHAT, "admin", "⚠️ Не добавлено", _rows(10))
    assert key == "", f"ключ снимка, которого нет: {key!r}"
    assert any("снимок «⚠️ Не добавлено» не записан" in r.getMessage() and "database is locked" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


def test_one_row_over_the_byte_limit_is_not_written_at_all(db, clock, caplog):
    """Единственная строка больше 256 КБ в базу не идёт: это сбой записи
    (строка журнала, пустой ключ), а не запись сверх предела."""
    with caplog.at_level(logging.ERROR, logger="awgbot.db"):
        key = db.list_snap_keep(CHAT, "admin", "t", ["ж" * (300 * 1024)])
    assert key == "" and not _snap_keys(db), (key, _snap_keys(db))
    assert any("снимок «t» не записан" in r.getMessage() and "256 КБ" in r.getMessage() for r in caplog.records)


def test_a_miss_at_opening_leaves_a_log_line_with_the_key_and_the_chat(db, clock, caplog):
    """«📋 Этого списка больше нет» по экрану не разобрать: промах — строкой
    журнала с ключом и чатом (не создавался / из другого чата)."""
    real = db.list_snap_keep(CHAT, "admin", "t", _rows(1))
    with caplog.at_level(logging.INFO, logger="awgbot.db"):
        assert db.list_snap_get("zzzzzzzzzz", CHAT) is None
        assert db.list_snap_get(real, OTHER) is None
    log = [r.getMessage() for r in caplog.records if "lists:" in r.getMessage()]
    assert any(f"снимок zzzzzzzzzz не найден (чат {CHAT})" in ln for ln in log), log
    assert any(f"снимок {real} — из другого чата, открывал {OTHER}" in ln for ln in log), log


def test_the_producer_helper_writes_nothing_without_a_bot_username(db, clock):
    """Без имени бота ссылки в тексте не будет (deep_link даёт текст) — снимок в
    базу не идёт, место в двадцати снимках чата не занимает; с именем — параметр
    /start на записанный снимок."""
    from awgbot.bot.lists.snapshot import snap_param_sync
    assert snap_param_sync(db, "", CHAT, "admin", "t", _rows(30)) == "" and not _snap_keys(db)
    param = snap_param_sync(db, "test_bot", CHAT, "admin", "t", _rows(30), n=40, missing=10, tail_note="остальные там")
    assert _KEY.fullmatch(param.removeprefix("list-snap-")), param
    got = db.list_snap_get(param.removeprefix("list-snap-"), CHAT)
    assert got["n"] == 40 and got["rows"][-1] == "… и ещё 10 — остальные там", got["rows"][-1]


def test_a_broken_snapshot_record_is_read_as_no_snapshot(db, clock):
    db._connection().execute("INSERT INTO server_state (key, value) VALUES (?, ?)",
                             (listsnap.PREFIX + "brokenkey1", "{не json"))
    db._connection().commit()
    assert db.list_snap_get("brokenkey1", CHAT) is None
    key = db.list_snap_keep(CHAT, "admin", "x", _rows(1))                  # уборка битую запись переживает
    assert db.list_snap_get(key, CHAT)


# ── вид snap: шапка, листание, «⬅️ Назад» ───────────────────────────────────

async def _open(db, key, *, role="admin", chat=CHAT, page=0):
    return await lists.render(_Svc(db), "snap", role=role, key=key, chat_id=chat, page=page)


async def test_the_head_is_title_count_page_and_time_of_the_event(db, clock):
    key = db.list_snap_keep(CHAT, "admin", "⚠️ Не добавлено", _rows(40), RoutingCB(action="sites", ref=3).pack(),
                            False, 52)
    clock.advance(3 * DAY)                                  # открыли позже — время в шапке всё равно события
    text, _ = await _open(db, key)
    head = text.split("\n", 1)[0]
    assert head == f"⚠️ <b>Не добавлено</b> · 52 · стр. 1 из 2 · от {timeutil.fmt_dt_ui(T0)}", head
    assert re.search(r"от \d\d\.\d\d \d\d:\d\d \(UTC[+-]\d+\)$", head), f"время без метки пояса: {head}"


async def test_one_page_has_no_page_label(db, clock):
    key = db.list_snap_keep(CHAT, "admin", "⚠️ Не добавлено", _rows(6))
    text, markup = await _open(db, key)
    assert text.split("\n", 1)[0] == f"⚠️ <b>Не добавлено</b> · 6 · от {timeutil.fmt_dt_ui(T0)}", text[:120]
    assert [t for t, _ in _buttons(markup)] == ["⬅️ В меню"], _buttons(markup)


async def test_pages_together_are_the_whole_snapshot_once_and_in_order(db, clock):
    src = _rows(75)
    key = db.list_snap_keep(CHAT, "admin", "t", src)
    first, _ = await _open(db, key)
    total = int(re.search(r"стр\. 1 из (\d+)", first).group(1))
    seen = []
    for p in range(total):
        text, markup = await _open(db, key, page=p)
        assert f"стр. {p + 1} из {total}" in text.split("\n", 1)[0], text[:100]
        seen += [ln for ln in text.split("\n")[2:] if ln]
        labels = [t for t, _ in _buttons(markup)]
        assert ("◀️ Пред. страница" in labels) == (p > 0) and ("След. страница ▶️" in labels) == (p < total - 1), \
            f"стр. {p + 1}: {labels}"
    assert seen == src, "строки потеряны или повторены на стыке страниц"


async def test_back_goes_to_the_source_screen(db, clock):
    back = RoutingCB(action="sites", ref=3).pack()
    key = db.list_snap_keep(CHAT, "client", "t", _rows(3), back)
    _, markup = await _open(db, key, role="client")
    assert _buttons(markup) == [("⬅️ Назад", back)], _buttons(markup)


@pytest.mark.parametrize("role, menu", [
    ("admin", Menu(action="main").pack()),
    ("client", Menu(action="main").pack()),
    ("invited", FriendCB(action="refresh").pack()),
])
async def test_without_a_source_the_menu_button_leads_to_the_viewers_own_main(db, clock, role, menu):
    """«⬅️ В меню» — на главную того, кто смотрит: у гостя она своя (FriendCB), у
    пользователя и админа — Menu."""
    key = db.list_snap_keep(CHAT, "admin", "t", _rows(3))
    _, markup = await _open(db, key, role=role)
    assert _buttons(markup) == [("⬅️ В меню", menu)], _buttons(markup)


async def test_the_agents_snapshot_without_a_source_leads_to_its_panel(db, clock, monkeypatch):
    monkeypatch.setattr(config, "ROLE", "gateway")
    key = db.list_snap_keep(CHAT, "admin", "🔀 Итог: в туннель", _rows(3))
    text, markup = await _open(db, key)
    assert text.startswith("🔀 <b>Итог: в туннель</b> · 3"), text[:80]
    assert _buttons(markup) == [("⬅️ В меню", GwCB(action="panel").pack())], _buttons(markup)


async def test_a_stale_snapshot_says_so_with_the_time_and_the_menu(db, clock):
    key = db.list_snap_keep(CHAT, "admin", "t", _rows(3), RoutingCB(action="sites", ref=3).pack())
    clock.advance(31 * DAY)
    text, markup = await _open(db, key)
    assert text == (f"📋 Список от {timeutil.fmt_dt_ui(T0)} больше не хранится — списки событий бот хранит "
                    "30 дней"), text
    assert _buttons(markup) == [("⬅️ В меню", Menu(action="main").pack())], \
        f"у протухшего — «Назад» в источник или нет выхода: {_buttons(markup)}"


async def test_an_evicted_snapshot_says_it_was_pushed_out_not_that_it_expired(db, clock):
    """Вытесненный 21-м снимок того же дня — не «хранит 30 дней» (это было бы
    неправдой), а «держит 20 последних списков чата»."""
    keys = [db.list_snap_keep(CHAT, "admin", "t", _rows(1)) for _ in range(21)]
    text, _ = await _open(db, keys[0])
    assert text == (f"📋 Список от {timeutil.fmt_dt_ui(T0)} больше не хранится — бот держит "
                    f"{listsnap.PER_CHAT} последних списков чата"), text
    assert db.list_snap_get(keys[0], CHAT) == {"stale": True, "at": timeutil.to_iso(T0), "why": "evicted"}


@pytest.mark.parametrize("chat, key", [(OTHER, None), (CHAT, "zzzzzzzzzz")])
async def test_a_foreign_or_unknown_snapshot_is_simply_gone(db, clock, chat, key):
    real = db.list_snap_keep(CHAT, "admin", "t", _rows(3))
    text, markup = await _open(db, key or real, chat=chat)
    assert text == "📋 Этого списка больше нет", text
    assert _buttons(markup) == [("⬅️ В меню", Menu(action="main").pack())], _buttons(markup)


# ── пользователь открывает снимок своего итога «Свои сайты» ─────────────────

async def test_a_user_opens_the_snapshot_of_his_own_sites_report(services, make_active_client, fake_bot,
                                                                monkeypatch):
    """Пользователь вставил 40 неадресов: в итоге «и ещё 35» — ссылкой на снимок; по
    ссылке — все 40 отказов, «⬅️ Назад» — на его «📋 Свои сайты»."""
    from awgbot.bot.handlers import client as ch
    from awgbot.bot.handlers import routing as routing_h
    from aiogram.filters import CommandObject
    from tests.conftest import FakeMessage, FakeState
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    services.bot_username = "test_bot"
    c = make_active_client(tg_id=7101, name="Вася")
    services.set_routing_allowed(c.id, True)
    services.set_routing_all(c.id, True)
    c = services.db.get_client(c.id)
    st = FakeState()
    await st.update_data(rt_client=c.id)
    msg = FakeMessage(text="\n".join(f"bad{i:02d}..ru" for i in range(40)), chat_id=7101, user_id=7101, bot=fake_bot)
    await routing_h.routing_add_apply(msg, c, services, st)
    report = [s for s in msg.sent if s[0] == "answer"][-1][1]
    m = re.search(r'<a href="https://t\.me/test_bot\?start=list-snap-([a-z0-9]{10})">и ещё 35</a>', report)
    assert m, f"хвост отказов — не ссылка на снимок: {report[:600]}"
    assert not re.search(r"start=[^\"]*-n\"", m.group(0)), "у итога на живом меню — метка «из уведомления»"
    key = m.group(1)
    snap = services.db.list_snap_get(key, 7101)
    assert snap and snap["n"] == 40 and len(snap["rows"]) == 40, snap
    opened = FakeMessage(text=f"/start list-snap-{key}", chat_id=7101, user_id=7101, bot=fake_bot)
    await ch.start_client_with_code(opened, CommandObject(prefix="/", command="start", args=f"list-snap-{key}"),
                                    c, services, FakeState())
    shown = [s for s in opened.sent if s[0] == "answer"] + \
        [("edit", r[2], None) for r in fake_bot.records if r[0] == "edit_message_text"]
    texts = [s[1] for s in shown if s[1] and "Не добавлено" in s[1]]
    assert texts and texts[-1].startswith("⚠️ <b>Не добавлено</b> · 40 · стр. 1 из 2"), shown
    assert "<code>bad00..ru</code>" in texts[-1], texts[-1][:300]


# ── бэкап и пакет резерва ───────────────────────────────────────────────────

def test_the_backup_copy_of_the_base_has_no_snapshots_but_keeps_the_rest(services, db, clock):
    """Снимки — следы интерфейса; копия — о данных: соседние ключи состояния на месте."""
    db.list_snap_keep(CHAT, "admin", "t", _rows(5))
    db.list_snap_keep(OTHER, "admin", "t", _rows(5))
    db.set_state("email_login", "box@example.org")
    raw = services.db_snapshot_bytes(db.path)
    p = db.path + ".copy"
    with open(p, "wb") as f:
        f.write(raw)
    con = sqlite3.connect(p)
    keys = {r[0] for r in con.execute("SELECT key FROM server_state")}
    con.close()
    assert not [k for k in keys if k.startswith("list_snap:")], f"снимки в копии: {sorted(keys)}"
    assert "email_login" in keys, "вырезание снимков задело остальное состояние"
    assert len(_snap_keys(db)) == 2, "снимки вырезаны из живой базы, а не из копии"


def test_the_backup_file_has_no_snapshots(services, db, clock, monkeypatch, tmp_path):
    from awgbot.util import secrets_util
    import io
    import tarfile
    db.list_snap_keep(CHAT, "admin", "t", _rows(5))
    monkeypatch.setattr(config, "DB_PATH", db.path)
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    services.backup_set_passphrase("correct horse")
    path = services.make_backup()[0]
    blob = secrets_util.decrypt(open(path, "rb").read(), passphrase="correct horse")
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        raw = tar.extractfile("state/bot.db").read()
    p = tmp_path / "restored.db"
    p.write_bytes(raw)
    con = sqlite3.connect(p)
    left = con.execute("SELECT COUNT(*) FROM server_state WHERE substr(key, 1, 10) = 'list_snap:'").fetchone()[0]
    con.close()
    assert left == 0, f"в файле бэкапа {left} снимков"


def test_the_reserve_package_carries_no_snapshots(db, clock, tmp_path):
    from awgbot.util.rsvpkg import bot
    db.list_snap_keep(CHAT, "admin", "t", _rows(5))
    raw = bot.replica(db.path)
    bot.assert_clean(raw)
    p = tmp_path / "replica.db"
    p.write_bytes(raw)
    con = sqlite3.connect(p)
    keys = [r[0] for r in con.execute("SELECT key FROM server_state")]
    con.close()
    assert not [k for k in keys if k.startswith("list_snap:")], keys


def test_the_package_guard_refuses_a_replica_with_a_snapshot(db, clock, tmp_path):
    from awgbot.util.rsvpkg import bot
    p = tmp_path / "replica.db"
    p.write_bytes(bot.replica(db.path))
    con = sqlite3.connect(p)
    con.execute("INSERT INTO server_state (key, value) VALUES ('list_snap:aaaaaaaaaa', '{}')")
    con.commit()
    con.close()
    with pytest.raises(bot.BotPartError):
        bot.assert_clean(p.read_bytes())


# ── обрезка по объёму и хвост вызывающего ────────────────────────────────────

def _cut_log(caplog, key: str) -> tuple[list[str], list[str]]:
    """(строки журнала порций снимка key, все отрезанные строки из них по порядку)."""
    import ast
    portions = [r.getMessage() for r in caplog.records
                if r.getMessage().startswith(f"lists: снимок {key} — не вошли")]
    rows: list[str] = []
    for ln in portions:
        rows += ast.literal_eval(ln.split("): ", 1)[1])
    return portions, rows


def test_a_cut_by_the_byte_limit_puts_every_cut_row_into_the_log_in_portions(db, clock, caplog):
    """Снимок упёрся не в 2000 строк, а в 256 КБ: «полный список в журнале
    бота» и тогда правда — каждая отрезанная строка в журнале ровно один раз,
    по порядку, порциями по LOG_TAIL_ROWS (строка — первые LOG_ROW_CHARS знаков)."""
    src = [f"<code>{i:04d}</code> " + "ж" * 900 for i in range(400)]       # ≈ 720 КБ в UTF-8
    with caplog.at_level(logging.INFO, logger="awgbot.db"):
        key = db.list_snap_keep(CHAT, "admin", "⚠️ Не добавлено", src)
    rows = db.list_snap_get(key, CHAT)["rows"]
    m = re.fullmatch(r"… и ещё (\d+) — полный список в журнале бота", rows[-1])
    assert m, rows[-1][:80]
    kept, cut = len(rows) - 1, int(m.group(1))
    assert kept + cut == 400 and cut > listsnap.LOG_TAIL_ROWS, (kept, cut)
    portions, logged = _cut_log(caplog, key)
    assert logged == [r[:listsnap.LOG_ROW_CHARS] for r in src[kept:]], \
        f"в журнале {len(logged)} отрезанных строк из {cut}, или не те, или не по порядку"
    assert len(portions) == -(-cut // listsnap.LOG_TAIL_ROWS), f"порций {len(portions)} на {cut} строк"
    head = [r.getMessage() for r in caplog.records if f"(снимок {key}, чат {CHAT})" in r.getMessage()]
    assert head == [f"lists: «⚠️ Не добавлено» (снимок {key}, чат {CHAT}) — 400 строк, в снимок вошли {kept}, "
                    "остальные — ниже"], head


def test_a_whole_snapshot_leaves_no_cut_lines_in_the_log(db, clock, caplog):
    """Влезло целиком — журнал о снимке молчит: строка «остальные — ниже» без
    порций сбила бы того, кто ищет хвост."""
    with caplog.at_level(logging.INFO, logger="awgbot.db"):
        key = db.list_snap_keep(CHAT, "admin", "t", _rows(300))
    assert not [r for r in caplog.records if key in r.getMessage()], [r.getMessage() for r in caplog.records]


def test_the_callers_tail_note_ends_a_cut_made_by_the_storage_and_the_cut_still_goes_to_the_log(db, clock, caplog):
    """tail_note (текст вызывающего о том, где остальное) — и тогда, когда режет
    само хранилище: строка хвоста кончается им, число в ней — все не вошедшие,
    а отрезанное здесь всё равно уходит в журнал этого бота."""
    src = _rows(2100)
    with caplog.at_level(logging.INFO, logger="awgbot.db"):
        key = db.list_snap_keep(CHAT, "admin", "t", src, n=2100, tail_note="в журнале бота на «Beta»")
    rows = db.list_snap_get(key, CHAT)["rows"]
    assert len(rows) == listsnap.MAX_ROWS and rows[:-1] == src[:1999], len(rows)
    assert rows[-1] == "… и ещё 101 — в журнале бота на «Beta»", rows[-1]
    _portions, logged = _cut_log(caplog, key)
    assert logged == src[1999:], f"отрезанное хранилищем не в журнале: {len(logged)} строк из 101"


def test_a_source_short_of_rows_and_cut_by_the_storage_counts_both_but_logs_only_what_it_had(db, clock, caplog):
    """Источник отдал 2100 из 2200, хранилище оставило 1999: в хвосте — все 201
    не вошедшие, и честно про обе части (сотни, которых не дали, в журнале нет;
    отрезанные здесь — есть), а в журнал ушли ровно 101 отрезанная строка."""
    src = _rows(2100)
    with caplog.at_level(logging.INFO, logger="awgbot.db"):
        key = db.list_snap_keep(CHAT, "admin", "t", src, n=2200, missing=100)
    rows = db.list_snap_get(key, CHAT)["rows"]
    assert rows[-1] == "… и ещё 201 — 100 в снимок не вошли, остальные — в журнале бота", rows[-1]
    _portions, logged = _cut_log(caplog, key)
    assert logged == src[1999:], len(logged)


def test_the_callers_tail_note_wins_over_the_combined_line_when_short_and_cut(db, clock, caplog):
    """Источник недодал (missing) и хранилище обрезало — но вызывающий назвал, где
    остальное (Т-Ж4: «в журнале бота на «Beta»»): хвост — ровно его текст.
    Объединённая строка «N в снимок не вошли, остальные — в журнале бота» здесь
    отправила бы админа искать спорные правки в журнале не того бота."""
    src = _rows(2100)
    note = "в журнале бота на «Beta»"
    with caplog.at_level(logging.INFO, logger="awgbot.db"):
        key = db.list_snap_keep(CHAT, "admin", "t", src, n=2200, missing=100, tail_note=note)
    rows = db.list_snap_get(key, CHAT)["rows"]
    assert len(rows) == listsnap.MAX_ROWS and rows[:-1] == src[:1999], len(rows)
    assert rows[-1] == f"… и ещё 201 — {note}", rows[-1]
    _portions, logged = _cut_log(caplog, key)
    assert logged == src[1999:], f"отрезанное хранилищем не в журнале: {len(logged)} строк из 101"

def test_one_row_over_the_byte_limit_among_short_ones_still_is_not_written(db, clock, caplog):
    """Строка больше 256 КБ первой — снимок из короткого остатка не собрать (в
    снимок входят только первые строки): сбой записи, базе ничего, журналу —
    причина, а не порции отрезанного."""
    with caplog.at_level(logging.INFO, logger="awgbot.db"):
        key = db.list_snap_keep(CHAT, "admin", "t", ["ж" * (300 * 1024)] + _rows(5))
    assert key == "" and not _snap_keys(db), (key, _snap_keys(db))
    msgs = [r.getMessage() for r in caplog.records]
    assert any("снимок «t» не записан: одна строка не влезает в 256 КБ" in m for m in msgs), msgs
    assert not any("не вошли (строки" in m for m in msgs), "порции журнала у снимка, которого нет"


# ── производители без имени бота ─────────────────────────────────────────────

class _Msg:
    def __init__(self, chat: int):
        self.chat = type("Chat", (), {"id": chat})()


async def test_without_a_bot_username_neither_producer_writes_a_snapshot(db, clock):
    """Имени бота нет (getMe не ответил на старте) — ссылки в хвосте не будет
    всё равно: ни snap_param, ни snap_codes в базу не пишут и параметра не
    дают, хвост остаётся текстом."""
    from awgbot.bot.lists.snapshot import snap_codes, snap_param
    svc = _Svc(db)
    svc.bot_username = ""
    assert await snap_param(svc, CHAT, "admin", "t", _rows(40)) == ""
    assert await snap_codes(_Msg(CHAT), svc, "➕ Добавлено", [f"10.0.0.{i}" for i in range(30)], "", 12) is None
    assert not _snap_keys(db), f"без имени бота записаны снимки: {_snap_keys(db)}"


async def test_snap_codes_writes_only_past_the_shown_and_escapes_every_item(db, clock):
    """Адресов не больше показанных — снимка нет (всё видно в итоге); больше —
    снимок всех строками <code> с экранированием и «⬅️ Назад» на экран ввода."""
    from awgbot.bot.lists.snapshot import snap_codes
    svc = _Svc(db)
    svc.bot_username = "test_bot"
    items = [f"h{i}.example" for i in range(12)]
    assert await snap_codes(_Msg(CHAT), svc, "➕ Добавлено", items, "s:fw", 12) is None and not _snap_keys(db)
    items.append("a<b>&c")
    got = await snap_codes(_Msg(CHAT), svc, "➕ Добавлено", items, "s:fw", 12)
    assert got is not None and got[0] == "test_bot", got
    snap = db.list_snap_get(got[1].removeprefix("list-snap-"), CHAT)
    assert snap["rows"][-1] == "<code>a&lt;b&gt;&amp;c</code>" and len(snap["rows"]) == 13, snap["rows"][-3:]
    assert snap["back"] == "s:fw", snap["back"]


# ── отчёт рассылки: снимок недоставленных ───────────────────────────────────

def _cl(i: int):
    return type("Cl", (), {"id": i, "tg_id": 1000 + i, "name": f"Профиль {i}"})()


async def test_the_broadcast_report_writes_no_undelivered_snapshot_when_only_guests_failed(db, clock):
    """Не дошло троим гостям: в отчёте — число, снимка недоставленных нет (в нём
    была бы одна строка «3 адресата без профиля», а ссылка — на неё)."""
    from awgbot.bot.handlers.admin.broadcast import send
    svc = _Svc(db)
    svc.bot_username = "test_bot"
    aud, und = await send._report_snaps(svc, CHAT, [_cl(1), _cl(2)], [9001, 9002, 9003], "test_bot")
    assert (aud, und) == ("", ""), (aud, und)
    assert not _snap_keys(db), "у одних гостей записан снимок недоставленных"


async def test_three_named_and_guests_undelivered_get_one_snapshot_with_everyone(db, clock):
    """Трое с профилем и двое гостей: хвост «и ещё 2» — ссылкой с «-n» на снимок
    всех пятерых (три профиля и строка гостей), в шапке — 5."""
    from awgbot.bot.handlers.admin.broadcast import send
    svc = _Svc(db)
    svc.bot_username = "test_bot"
    clients = [_cl(1), _cl(2), _cl(3)]
    aud, und = await send._report_snaps(svc, CHAT, clients, [1001, 1002, 1003, 9001, 9002], "test_bot")
    m = re.fullmatch(r' <a href="https://t\.me/test_bot\?start=list-snap-([a-z0-9]{10})">и ещё 2</a>', und)
    assert aud == "" and m, (aud, und)
    snap = db.list_snap_get(m.group(1), CHAT)
    assert snap["n"] == 5 and snap["new"] is True, snap
    assert len(snap["rows"]) == 4 and snap["rows"][-1] == "2 адресата без профиля — те, с кем делятся устройствами", \
        snap["rows"]


# ── «Свои сайты»: снимок — по тем же группам причин, что итог ───────────────

async def _sites_report(services, make_active_client, fake_bot, monkeypatch, rejected):
    from types import SimpleNamespace
    from awgbot.bot.handlers import routing as routing_h
    from tests.conftest import FakeMessage, FakeState
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    services.bot_username = "test_bot"
    c = make_active_client(tg_id=7102, name="Петя")
    services.set_routing_allowed(c.id, True)
    services.set_routing_all(c.id, True)
    c = services.db.get_client(c.id)
    monkeypatch.setattr(services, "routing_add_domains",
                        lambda cid, text: SimpleNamespace(added=[], rejected=list(rejected), over_limit=0, limit=100))
    st = FakeState()
    await st.update_data(rt_client=c.id)
    msg = FakeMessage(text="x", chat_id=7102, user_id=7102, bot=fake_bot)
    await routing_h.routing_add_apply(msg, c, services, st)
    return [s for s in msg.sent if s[0] == "answer"][-1][1]


async def test_twelve_rejections_spread_over_three_reasons_need_no_snapshot(services, make_active_client, fake_bot,
                                                                           monkeypatch):
    """12 отказов, по 4 на причину: каждая причина в итоге целиком, хвостов «и
    ещё N» нет — снимка тоже нет (решение по сумме отказов писало бы его зря)."""
    rejected = [(f"r{k}{i}", reason) for k, reason in enumerate(("не похоже на адрес", "адрес локальной сети",
                                                                  "уже в общем списке")) for i in range(4)]
    report = await _sites_report(services, make_active_client, fake_bot, monkeypatch, rejected)
    assert "и ещё" not in report and "list-snap-" not in report, report
    assert not _snap_keys(services.db), "снимок отказов, хотя каждая причина видна целиком"


async def test_six_rejections_of_one_reason_among_others_get_the_snapshot_link(services, make_active_client, fake_bot,
                                                                              monkeypatch):
    """Шесть отказов одной причины (вторая по счёту в итоге) — у её строки «и ещё 1»
    ссылкой на снимок всех отказов; у причины, что влезла, хвоста нет."""
    rejected = [("a..ru", "не похоже на адрес")] + [(f"10.0.0.{i}", "адрес локальной сети") for i in range(6)]
    report = await _sites_report(services, make_active_client, fake_bot, monkeypatch, rejected)
    lines = [ln for ln in report.split("\n") if ln.startswith("⚠️ Не добавлено:")]
    assert len(lines) == 2 and "и ещё" not in lines[0], lines
    m = re.search(r'start=list-snap-([a-z0-9]{10})">и ещё 1</a> — адрес локальной сети$', lines[1])
    assert m, lines[1]
    assert len(services.db.list_snap_get(m.group(1), 7102)["rows"]) == 7
