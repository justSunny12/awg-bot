"""База бота основы на резерве по журналу (util/rsvjrn/opener.py) и отметки
снимка (util/rsvpkg/bot.py replica_marked).

Процесс открытия кладёт гостю базу: снимок `pkg3` + события основы после
снимка из хранилища резерва (подряд, до разрыва) + журнал данных гостя (если
резерв перезагружался посреди обслуживания) + пачки счётчиков основы
(сторона a, после учтённых снимком) и гостя (b). Затем строка эпохи: журнал
гостя есть — гость продолжает свою эпоху; переезд кнопкой — следует за
основой; авария — эпоха основы + 1, держатель.

Мир: основа — живая база теста (сервисы поверх неё), захват включён; хранилища
резерва и гостя — каталоги во временной папке, ключ `K` — случайный.

Цена ошибки: отметки снимка не из той же копии базы, что реплика, — резерв
снимает события, которых в снимке нет (или наоборот, гость применит их
дважды); события после разрыва, применённые поверх дыры, — база гостя с
решениями без их предпосылок; журнал гостя, не поднятый после перезагрузки
резерва, — правки людей на резерве пропадают; эпоха гостя не та — при
переезде кнопкой гость пишет решения раньше, чем основа отдала бота (два
писателя), в аварию — не пишет вовсе; номера гостя, переиспользованные
после открытия, — основа примет его новые правки за повтор и отбросит.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from awgbot.core import config
from awgbot.infra.db import Database
from awgbot.util.rsvjrn import counts as jcounts
from awgbot.util.rsvjrn import event, opener
from awgbot.util.rsvjrn.segments import Store
from awgbot.util.rsvpkg import bot as pkgbot
from tests import jrnequiv

pytestmark = pytest.mark.integration

_FUTURE = "2099-01-01T00:00:00+03:00"


class World:
    """Основа с захватом, хранилище резерва и журнал гостя на диске."""

    def __init__(self, svc, tmp_path):
        self.svc, self.db, self.tmp = svc, svc.db, tmp_path
        self.k = os.urandom(32)
        self.keep = Store(tmp_path / "keep")
        self.mine = Store(tmp_path / "guest-jrn")
        self.line = self.db.jrn_line()
        self.db.jrn_capture_set(True)
        self.n = 0

    # ── основа ──
    def edit(self, name: str = "") -> int:
        """Правка решения у основы: новый профиль с устройством — несколько событий."""
        self.n += 1
        cid = self.db.create_client(name or f"Профиль {self.n}", 3, "2026-01-01T00:00:00+03:00", _FUTURE,
                                    f"INV-DUMMY-{self.n}")
        self.db.create_device(cid, "Телефон", f"DUMMY-pub-{self.n}", "DUMMY-psk", f"10.8.1.{100 + self.n}")
        return cid

    def snapshot(self) -> tuple[bytes, dict]:
        """Реплика и отметки — как их кладёт пакет: из той же копии, плюс номер последней
        пачки счётчиков, строки которой в снимке (cb)."""
        rep, marks = pkgbot.replica_marked(self.db.path)
        marks["cb"] = self.db.jrn_counts_bn()
        self.db.jrn_counts_cover(marks["cs"], marks["cb"])      # как после pkg_ok снимка
        return rep, marks

    def ship(self, after: int, cs: int = 0) -> None:
        """События основы после `after` и её дельты после `cs` — в хранилище резерва
        (как их положил бы бот резерва из `jev` и `jcnt`)."""
        evs = self.db.jrn_events_after(after, 100_000)
        by_e: dict[int, list[bytes]] = {}
        for ev in evs:
            by_e.setdefault(ev.e, []).append(event.seal_event(self.k, self.line, ev))
        for e in sorted(by_e):
            self.keep.append_events(by_e[e])
        batch = self.db.jrn_counts_out(f"{self.line}:a")
        if batch is not None:
            self.keep.append_counts("a", [event.seal_counts(self.k, self.line, "a", batch)])
            self.db.jrn_counts_acked(batch.bn)

    # ── резерв ──
    def open(self, rep: bytes, marks: dict, *, button: bool = False, name: str = "guest.db") -> tuple[str, opener.Opened]:
        path = str(self.tmp / name)
        with open(path, "wb") as f:
            f.write(rep)
        res = opener.build(path, self.k, marks, self.keep.path, self.mine.path, button=button)
        return path, res


@pytest.fixture()
def w(services, tmp_path):
    return World(services, tmp_path)


def _diff(a_db, path: str) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return jrnequiv.diff(a_db._connection(), con)
    finally:
        con.close()


def _meta(path: str) -> dict:
    con = sqlite3.connect(path)
    try:
        cols = ("epoch", "holder", "role", "capture", "applied", "acked", "fork_i")
        row = con.execute("SELECT " + ", ".join(cols) + " FROM jrn_meta WHERE id = 1").fetchone()
        return dict(zip(cols, row))
    finally:
        con.close()


def _one(path: str, sql: str, args=()):
    con = sqlite3.connect(path)
    try:
        row = con.execute(sql, args).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _traffic(conn) -> list[tuple]:
    return [tuple(r) for r in conn.execute(
        "SELECT device_id, traffic_rx_month, traffic_tx_month, rf_rx_month, rf_tx_month FROM device_traffic "
        "ORDER BY device_id")]


# ── отметки снимка ──────────────────────────────────────────────────────────

def test_the_marks_come_from_the_same_copy_as_the_replica(w):
    """`i` — последний номер события в копии, `cs` — последней дельты; правка
    события `i` в реплике уже есть, событий и дельт журнала в ней нет."""
    w.edit("Иван")
    did = w.db.list_all_devices()[-1].id
    w.db.add_traffic_bulk([(did, 100, 200)])
    w.db.update_client_fields(w.db.list_clients()[-1].id, name="Иван Петров")
    rep, marks = w.snapshot()
    assert marks["i"] == w.db.jrn_last_i() > 0, marks
    assert marks["cs"] == w.db._connection().execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'jrn_counts'").fetchone()[0] > 0, marks
    assert marks["line"] == w.line and marks["e"] == w.db.jrn_meta()["epoch"], marks
    path = str(w.tmp / "rep.db")
    with open(path, "wb") as f:
        f.write(rep)
    assert _one(path, "SELECT COUNT(*) FROM clients WHERE name = 'Иван Петров'") == 1, \
        "правка последнего события не в реплике, а резерв снимет его по отметке"
    for t in ("jrn_events", "jrn_tail", "jrn_counts", "jrn_seen", "jrn_meta"):
        assert _one(path, f"SELECT COUNT(*) FROM {t}") == 0, f"{t} уехал в снимок — журнал местный у каждой базы"
    assert _one(path, "SELECT seq FROM sqlite_sequence WHERE name = 'jrn_events'") == marks["i"], \
        "номер последнего события не уехал со снимком — гость начнёт номера заново"


def test_the_marks_survive_events_already_dropped_after_an_ack(w):
    """Подтверждённые события удалены — номер снимка всё равно последний выданный
    (номера не переиспользуются)."""
    w.edit()
    last = w.db.jrn_last_i()
    w.db.jrn_acked_set(last)
    assert w.db.jrn_events_after(0) == []
    _rep, marks = w.snapshot()
    assert marks["i"] == last, marks


def test_a_base_without_events_gives_zero_marks(services):
    _rep, marks = pkgbot.replica_marked(services.db.path)
    assert (marks["i"], marks["cs"]) == (0, 0), marks
    assert marks["line"] == services.db.jrn_line() != ""


# ── открытие ────────────────────────────────────────────────────────────────

def test_snapshot_events_and_counts_give_the_base_of_the_primary(w):
    """Снимок + события + пачки = база основы на момент последнего события (по
    решениям, маскам и данным хоста) и её счётчики."""
    w.edit("Иван")
    ivan_dev = w.db.list_all_devices()[-1].id
    w.db.add_traffic_bulk([(ivan_dev, 1000, 2000)])
    rep, marks = w.snapshot()
    # после снимка: новые профили, правка, блокировка, удаление, дельты
    w.edit("Пётр")
    w.db.update_client_fields(w.db.list_clients()[-1].id, name="Пётр I")
    w.db.update_device_fields(ivan_dev, block_reason=1)
    gone = w.db.list_all_devices()[-1].id
    w.edit("Вера")
    w.db.delete_device(gone)
    w.db.add_traffic_bulk([(ivan_dev, 10, 20)])
    w.ship(marks["i"], marks["cs"])
    path, res = w.open(rep, marks)
    assert _diff(w.db, path) == [], "база гостя не равна базе основы"
    con = sqlite3.connect(path)
    try:
        assert _traffic(con) == _traffic(w.db._connection()), "счётчики гостя не равны счётчикам основы"
    finally:
        con.close()
    assert res.events == w.db.jrn_last_i() - marks["i"] and res.gap == 0 and res.lost == 0, res
    assert res.fork == w.db.jrn_last_i() and res.counts == 1, res
    assert _one(path, "SELECT value FROM server_state WHERE key = 'db_line'") == w.line, "линия гостя — не основы"


def test_counts_already_in_the_snapshot_are_not_added_twice(w):
    """Пачка a с дельтами до `cs` снимка не складывается второй раз."""
    w.edit()
    did = w.db.list_all_devices()[-1].id
    w.db.add_traffic_bulk([(did, 500, 500)])
    w.ship(0)                                            # пачка ушла резерву до снимка
    rep, marks = w.snapshot()
    path, res = w.open(rep, marks)
    con = sqlite3.connect(path)
    try:
        assert _traffic(con) == _traffic(w.db._connection()), "дельта, учтённая снимком, сложена ещё раз"
    finally:
        con.close()
    assert res.counts == 0, res


def test_events_after_a_gap_are_not_taken(w):
    """Дыра в номерах хранилища — берётся только то, что до неё, разрыв —
    в итоге; база гостя — основы на момент перед дырой."""
    rep, marks = w.snapshot()
    w.edit("Иван")
    upto = w.db.jrn_last_i()
    w.edit("Пётр")
    gap_from = upto + 1
    evs = w.db.jrn_events_after(marks["i"], 1000)
    w.keep.append_events([event.seal_event(w.k, w.line, ev) for ev in evs if ev.i != gap_from])
    path, res = w.open(rep, marks)
    assert res.gap == gap_from and res.fork == upto, res
    assert _one(path, "SELECT COUNT(*) FROM clients WHERE name = 'Пётр'") == 0, "события после разрыва применены"
    assert _one(path, "SELECT COUNT(*) FROM clients WHERE name = 'Иван'") == 1
    assert _meta(path)["applied"] == upto


def test_a_record_under_another_key_is_a_gap_not_garbage(w):
    """Запись хранилища не под `K` основы (порча, чужой ключ) — разрыв: открытие
    идёт по тому, что до неё, мусора в базе нет."""
    rep, marks = w.snapshot()
    w.edit("Иван")
    good = w.db.jrn_last_i()
    w.edit("Пётр")
    evs = w.db.jrn_events_after(marks["i"], 1000)
    w.keep.append_events([event.seal_event(w.k if ev.i <= good else os.urandom(32), w.line, ev) for ev in evs])
    path, res = w.open(rep, marks)
    assert res.gap == good + 1 and res.fork == good, res
    assert _one(path, "SELECT COUNT(*) FROM clients WHERE name = 'Пётр'") == 0, "событие под чужим ключом применено"
    assert _one(path, "SELECT COUNT(*) FROM clients WHERE name = 'Иван'") == 1


# ── строка эпохи ────────────────────────────────────────────────────────────

def test_an_emergency_open_holds_the_next_epoch(w):
    """Авария: эпоха основы + 1, держатель — сам гость, захват включён."""
    w.edit()
    rep, marks = w.snapshot()
    w.edit()
    w.ship(marks["i"])
    path, res = w.open(rep, marks, button=False)
    m = _meta(path)
    base_e = w.db.jrn_meta()["epoch"]
    assert (m["epoch"], m["holder"], m["role"], m["capture"]) == (base_e + 1, "self", "holder", 1), m
    assert m["applied"] == m["acked"] == m["fork_i"] == w.db.jrn_last_i(), m
    assert res.epoch == base_e + 1 and res.role == "holder"


def test_a_button_open_follows_the_primary_until_it_hands_over(w):
    """Переезд кнопкой: эпоха основы, держатель — основа, гость следует (пишет
    решения только после передачи бота)."""
    w.edit()
    rep, marks = w.snapshot()
    path, res = w.open(rep, marks, button=True)
    m = _meta(path)
    assert (m["epoch"], m["holder"], m["role"]) == (w.db.jrn_meta()["epoch"], "peer", "follower"), m
    g = Database(path)
    try:
        g.init_schema()
        with pytest.raises(sqlite3.IntegrityError, match="^jrn:"):
            g.create_client("Чужой", 1, "2026-01-01T00:00:00+03:00", _FUTURE, "INV-DUMMY-X")
    finally:
        g.close()


def _guest_edits(path: str, monkeypatch, n: int = 1) -> list:
    """Гость пишет решения в своей базе (держатель): его события — по номеру."""
    monkeypatch.setattr(config, "GUEST", True)
    g = Database(path)
    try:
        g.init_schema()
        before = g.jrn_last_i()
        for j in range(n):
            g.create_client(f"Гость {j}", 1, "2026-01-01T00:00:00+03:00", _FUTURE, f"INV-DUMMY-G{j}")
        return g.jrn_events_after(before, 1000)
    finally:
        g.close()
        monkeypatch.setattr(config, "GUEST", False)


def test_a_reserve_rebooted_while_serving_reopens_with_the_guest_journal(w, monkeypatch):
    """Перезагрузка резерва во время обслуживания: снимок + события основы +
    журнал данных гостя — правки людей на резерве целы, гость продолжает свою
    эпоху держателем, его номера дальше последнего."""
    w.edit("Иван")
    rep, marks = w.snapshot()
    w.edit("Пётр")
    w.ship(marks["i"])
    first, _res = w.open(rep, marks, name="first.db")
    g_epoch = _meta(first)["epoch"]
    gevs = _guest_edits(first, monkeypatch, n=2)
    assert gevs and gevs[0].i == w.db.jrn_last_i() + 1, "гость начал номера не с последнего основы + 1"
    w.mine.append_events([event.seal_event(w.k, w.line, ev) for ev in gevs])
    # резерв перезагрузился: открытие заново из того же снимка и хранилища
    again, res = w.open(rep, marks, name="again.db")
    assert _one(again, "SELECT COUNT(*) FROM clients WHERE name IN ('Гость 0', 'Гость 1')") == 2, \
        "правки людей на резерве пропали после перезагрузки"
    assert _one(again, "SELECT COUNT(*) FROM clients WHERE name = 'Пётр'") == 1
    m = _meta(again)
    assert (m["epoch"], m["holder"], m["role"]) == (g_epoch, "self", "holder"), m
    assert res.guest_events == len(gevs) and res.events == w.db.jrn_last_i() - marks["i"], res
    assert _one(again, "SELECT seq FROM sqlite_sequence WHERE name = 'jrn_events'") >= gevs[-1].i, \
        "номера гостя переиспользуются — основа примет новые правки за повтор"
    nxt = _guest_edits(again, monkeypatch)
    assert nxt[0].i == gevs[-1].i + 1, f"следующий номер гостя {nxt[0].i}, ждали {gevs[-1].i + 1}"


def test_the_guest_counts_side_b_are_taken_from_its_journal(w, monkeypatch):
    """Пачки клона (b) из журнала гостя ложатся в счётчики после перезагрузки."""
    w.edit()
    did = w.db.list_all_devices()[-1].id
    rep, marks = w.snapshot()
    before = _traffic(w.db._connection())
    batch = jcounts.Batch(host=f"{w.line}:b", bn=3, s0=10, s1=12, months={jcounts.current_month(w.db._connection()):
                                                            {did: [70, 80, 0, 0, None]}})
    w.mine.append_counts("b", [event.seal_counts(w.k, w.line, "b", batch)])
    path, res = w.open(rep, marks)
    con = sqlite3.connect(path)
    try:
        got = {r[0]: r for r in _traffic(con)}[did]
    finally:
        con.close()
    was = {r[0]: r for r in before}[did]
    assert (got[1] - was[1], got[2] - was[2]) == (70, 80), f"пачка клона не сложилась: {was} → {got}"
    assert res.counts == 1
    assert _one(path, "SELECT cnt_bn FROM jrn_meta WHERE id = 1") == 3, \
        "счёт пачек гостя после открытия начат заново — основа примет его пачки за повтор"


def test_the_epoch_known_to_the_reserve_raises_the_epoch_of_the_open(w):
    """Смена роли основы без событий подняла эпоху линии — резерв знает её (отметка
    в хранилище): аварийное открытие берёт следующую за ней, а не за эпохой снимка;
    отметка чужой линии не в счёт."""
    import json
    w.edit()
    rep, marks = w.snapshot()
    w.keep.path.mkdir(parents=True, exist_ok=True)
    (w.keep.path / "meta.json").write_text(json.dumps({"line": "00" * 8, "e": 40}), encoding="utf-8")
    _path, res = w.open(rep, marks, name="other.db")
    assert res.epoch == marks["e"] + 1, "эпоха чужой линии подняла эпоху открытия"
    (w.keep.path / "meta.json").write_text(json.dumps({"line": w.line, "e": marks["e"] + 4}), encoding="utf-8")
    path, res = w.open(rep, marks)
    assert res.epoch == marks["e"] + 5 and _meta(path)["epoch"] == marks["e"] + 5, res
