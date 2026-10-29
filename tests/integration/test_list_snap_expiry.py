"""Срок снимка списка (infra/db/listsnap/expiry, snaps) на настоящей базе и
замороженных часах: полночь пояса показа после 30 дней, запись срока в пояс
хранения, пояс с переходом времени, снимок 4.1.0 без `exp`, открытие за минуту
до полуночи и в полночь, такт `list_snap_expire_due`, и то, что записи
сообщений (`list_snap:m:…`) — не снимки ни для уборки, ни для ссылки.

Цена ошибки: срок «ровно 30 дней» — ссылка в чате умирает посреди дня, а такт
после полуночи о ней не знает: человек нажимает ссылку, которую бот ещё не
переписал, и видит «больше не хранится» без правки текста; срок по поясу
хранения при другом поясе показа — снимок умирает в 22:00 по часам человека;
срок, сдвинутый переходом времени, — на час раньше или позже полуночи; снимок
4.1.0 без `exp`, который не протухает (или протухает сразу), — база растёт
или ссылки ломаются у всех после обновления; такт, который не сжимает снимки
соседнего чата, — их ссылки не правятся никогда; уборка, принявшая запись
сообщения за снимок, — затирает запись отметкой, и «Скрыть» с ссылками
остаются в чате навсегда; ключ записи, открытый как снимок, — чужой текст
сообщения на экране «весь список».
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from zoneinfo import ZoneInfo

import pytest

from awgbot.bot import keyboards as kb
from awgbot.core import config
from awgbot.infra.db import listsnap
from awgbot.infra.db.listsnap import expiry
from awgbot.util import timeutil

pytestmark = pytest.mark.integration

CHAT, OTHER = 5550001, 5550002
UTC5 = config.TZ                                   # пояс показа тестов (conftest: Asia/Yekaterinburg, UTC+5)
T0 = _dt.datetime(2026, 10, 27, 14, 5, 0, tzinfo=UTC5)
DEATH = _dt.datetime(2026, 11, 27, 0, 0, 0, tzinfo=UTC5)
DAY = 86400
HIDE_KB = kb.hide_only().model_dump_json(exclude_none=True)


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


def _zone(monkeypatch, tz) -> None:
    """Пояс показа — tz (хранение остаётся UTC+3)."""
    monkeypatch.setattr(config, "TZ", tz)
    monkeypatch.setattr(timeutil, "TZ", tz)


def _rows(n: int, tag: str = "r") -> list[str]:
    return [f"<code>{tag}{i:04d}.example</code> — не похоже на адрес" for i in range(n)]


def _raw(db, key: str) -> dict | None:
    row = db._connection().execute("SELECT value FROM server_state WHERE key = ?",
                                   (listsnap.PREFIX + key,)).fetchone()
    return None if row is None else json.loads(row[0])


def _put_raw(db, key: str, body: dict) -> None:
    db.set_state(listsnap.PREFIX + key, json.dumps(body, ensure_ascii=False))


def _v1(at: _dt.datetime, chat: int = CHAT, seq: int = 1) -> dict:
    """Тело снимка 4.1.0: без `v`, `exp` и `msg`."""
    return {"chat": chat, "role": "admin", "at": timeutil.to_iso(at), "seq": seq, "title": "старый",
            "n": 2, "rows": _rows(2), "back": "", "new": False}


# ── правило срока ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("at", [
    _dt.datetime(2026, 10, 27, 14, 5, 0, tzinfo=UTC5),
    _dt.datetime(2026, 10, 27, 0, 0, 0, tzinfo=UTC5),
    _dt.datetime(2026, 10, 27, 23, 59, 59, tzinfo=UTC5),
], ids=["14:05", "00:00:00", "23:59:59"])
def test_a_snapshot_of_any_moment_of_the_day_dies_at_the_midnight_after_thirty_days(at):
    """Любой момент 27.10 по поясу показа — смерть ровно в 00:00 27.11: снимок
    живёт от 30 до 31 дня, и все ссылки одного дня умирают одним тактом."""
    got = expiry.expires_at(at)
    assert got == DEATH, f"создан {at}, умрёт {got}, ждали {DEATH}"
    assert got.astimezone(timeutil.TZ).time() == _dt.time(0, 0), f"смерть не в полночь пояса показа: {got}"


def test_the_stored_expiry_is_midnight_of_the_display_zone_written_in_the_storage_zone(db, clock):
    """Показ UTC+5, хранение UTC+3: `exp` в теле — «2026-11-26T22:00:00+03:00»
    (полночь по часам человека), а не полночь по поясу хранения."""
    key = db.list_snap_keep(CHAT, "admin", "x", _rows(2))
    raw = _raw(db, key)
    assert raw["v"] == 2 and raw["msg"] == 0, f"тело снимка не v2 или уже привязано: {raw}"
    assert raw["exp"] == "2026-11-26T22:00:00+03:00", f"срок записан не в пояс хранения или не та полночь: {raw['exp']}"


def test_the_day_is_taken_in_the_display_zone_not_in_the_storage_zone(db, monkeypatch):
    """27.10 01:00 по UTC+5 — это ещё 26.10 по UTC+3: день считается по поясу
    показа, снимок умирает 27.11 00:00 (UTC+5), а не на сутки раньше."""
    _Clock(monkeypatch, _dt.datetime(2026, 10, 27, 1, 0, 0, tzinfo=UTC5))
    key = db.list_snap_keep(CHAT, "admin", "x", _rows(2))
    assert timeutil.parse_iso(_raw(db, key)["exp"]) == DEATH, _raw(db, key)["exp"]


def test_changing_the_display_zone_after_creation_does_not_move_the_death(db, clock, monkeypatch):
    """Срок записан моментом: админ сменил пояс показа на UTC+0 — снимок всё равно
    умирает в тот же момент (27.11 00:00 по UTC+5), а не в полночь нового пояса."""
    key = db.list_snap_keep(CHAT, "admin", "x", _rows(2))
    _zone(monkeypatch, ZoneInfo("Europe/London"))
    clock.t = DEATH - _dt.timedelta(seconds=1)
    assert not db.list_snap_get(key, CHAT).get("stale"), "смена пояса показа сдвинула срок раньше"
    clock.t = DEATH
    assert db.list_snap_get(key, CHAT).get("stale"), "смена пояса показа сдвинула срок позже"


def test_a_display_zone_with_a_clock_change_inside_the_month_still_gives_true_midnight(monkeypatch):
    """Берлин: создан 10.10 (UTC+2), между ними 25.10 часы отходят на час — смерть
    10.11 00:00 по стенным часам (UTC+1), то есть 09.11 23:00 UTC, а не 22:00."""
    berlin = ZoneInfo("Europe/Berlin")
    _zone(monkeypatch, berlin)
    got = expiry.expires_at(_dt.datetime(2026, 10, 10, 12, 0, tzinfo=berlin))
    assert got.astimezone(_dt.timezone.utc) == _dt.datetime(2026, 11, 9, 23, 0, tzinfo=_dt.timezone.utc), \
        f"полночь после перехода времени съехала: {got.isoformat()}"
    assert expiry.exp_iso(timeutil.to_iso(_dt.datetime(2026, 10, 10, 12, 0, tzinfo=berlin))) == \
        "2026-11-10T02:00:00+03:00"


@pytest.mark.parametrize("zone, at", [
    ("America/Santiago", _dt.datetime(2026, 8, 6, 12, 0)),    # 06.09 полуночи нет: 00:00 → 01:00
    ("America/Havana", _dt.datetime(2026, 10, 1, 12, 0)),     # 01.11 полночь дважды: 01:00 → 00:00
], ids=["midnight-skipped", "midnight-repeated"])
def test_when_midnight_itself_is_skipped_or_repeated_death_is_the_first_moment_of_the_day(monkeypatch, zone, at):
    """Пояс, где часы переводят в полночь: смерть — первый момент нужного дня (за
    секунду до неё — ещё прежний день), а не час спустя и не накануне."""
    tz = ZoneInfo(zone)
    _zone(monkeypatch, tz)
    got = expiry.expires_at(at.replace(tzinfo=tz))
    day = at.date() + _dt.timedelta(days=31)
    before = _dt.datetime.fromtimestamp(got.timestamp() - 1, tz)
    assert got.astimezone(tz).date() == day, f"смерть не в тот день: {got.isoformat()}"
    assert before.date() == day - _dt.timedelta(days=1), \
        f"смерть не в первый момент дня: за секунду до неё уже {before.isoformat()}"


# ── открытие за минуту до полуночи и в полночь ─────────────────────────────

def test_opened_at_23_59_the_evening_before_it_is_a_snapshot_and_at_midnight_a_mark(db, clock):
    """Создан 27.10 14:05: 26.11 23:59 — строки на месте; 27.11 00:00 — «больше не
    хранится», и в базе на месте строк — отметка."""
    key = db.list_snap_keep(CHAT, "admin", "x", _rows(2))
    clock.t = _dt.datetime(2026, 11, 26, 23, 59, 0, tzinfo=UTC5)
    assert db.list_snap_get(key, CHAT)["rows"] == _rows(2), "снимок протух до полуночи"
    clock.t = DEATH
    assert db.list_snap_get(key, CHAT) == {"stale": True, "at": timeutil.to_iso(T0), "why": ""}
    assert _raw(db, key) == {"chat": CHAT, "at": timeutil.to_iso(T0), "gone": 1, "why": ""}, \
        f"протухший при открытии снимок не сжат в отметку: {_raw(db, key)}"


def test_a_snapshot_of_the_previous_release_without_exp_gets_the_same_term_from_at(db, clock):
    """Снимок 4.1.0 (без `exp`) после обновления: тот же срок, выведенный из `at`,
    — живой в 23:59 накануне, отметка в полночь."""
    _put_raw(db, "oldsnap001", _v1(T0))
    clock.t = _dt.datetime(2026, 11, 26, 23, 59, 0, tzinfo=UTC5)
    got = db.list_snap_get("oldsnap001", CHAT)
    assert got and got.get("rows") == _rows(2), f"снимок 4.1.0 не читается или протух раньше срока: {got}"
    clock.t = DEATH
    assert db.list_snap_get("oldsnap001", CHAT) == {"stale": True, "at": timeutil.to_iso(T0), "why": ""}, \
        "снимок 4.1.0 не протух в полночь после 30 дней"


def test_a_snapshot_of_the_previous_release_is_swept_by_the_same_rule_on_the_next_write(db, clock):
    """Уборка при записи сжимает v1 по сроку из `at`: в 23:59 — не трогает, в полночь — отметка."""
    _put_raw(db, "oldsnap001", _v1(T0))
    clock.t = _dt.datetime(2026, 11, 26, 23, 59, 0, tzinfo=UTC5)
    db.list_snap_keep(OTHER, "admin", "y", _rows(1))
    assert not _raw(db, "oldsnap001").get("gone"), "уборка сжала снимок 4.1.0 раньше срока"
    clock.t = DEATH
    db.list_snap_keep(OTHER, "admin", "z", _rows(1))
    assert _raw(db, "oldsnap001") == {"chat": CHAT, "at": timeutil.to_iso(T0), "gone": 1, "why": ""}


# ── такт после полуночи ─────────────────────────────────────────────────────

def test_the_nightly_pass_marks_expired_snapshots_of_every_chat_and_names_them(db, clock):
    """Такт в 27.11 00:00: снимки 27.10 обоих чатов (и v1) — в отметки, и такт
    называет их ключи с чатами (их сообщения — к правке); снимок 28.10 живёт."""
    mine = db.list_snap_keep(CHAT, "admin", "мой", _rows(1))
    theirs = db.list_snap_keep(OTHER, "admin", "соседа", _rows(1))
    _put_raw(db, "oldsnap001", _v1(T0, seq=99))
    clock.advance(DAY)
    young = db.list_snap_keep(CHAT, "admin", "завтрашний", _rows(1))
    got = db.list_snap_expire_due(DEATH)
    assert sorted(got) == sorted([(mine, CHAT), (theirs, OTHER), ("oldsnap001", CHAT)]), got
    for k in (mine, theirs, "oldsnap001"):
        assert _raw(db, k).get("gone") == 1 and _raw(db, k).get("why") == "", f"{k} не сжат в отметку: {_raw(db, k)}"
    assert not _raw(db, young).get("gone"), "такт сжал снимок, чей срок не наступил"


def test_a_second_nightly_pass_finds_nothing_new(db, clock):
    """Догон на старте после такта: уже сжатые не называются снова — сообщения
    не правятся дважды."""
    db.list_snap_keep(CHAT, "admin", "x", _rows(1))
    assert len(db.list_snap_expire_due(DEATH)) == 1
    assert db.list_snap_expire_due(DEATH) == [], "повторный такт снова назвал сжатый снимок"


def test_the_nightly_pass_before_midnight_touches_nothing(db, clock):
    key = db.list_snap_keep(CHAT, "admin", "x", _rows(1))
    assert db.list_snap_expire_due(DEATH - _dt.timedelta(seconds=1)) == [], "такт сжал снимок до полуночи"
    assert not _raw(db, key).get("gone")


def test_the_nightly_pass_does_not_name_evicted_marks_and_drops_marks_older_than_ninety_days(db, clock):
    """Отметка вытесненного — не новость для такта (её сообщение правили при
    вытеснении); отметка старше 90 дней — прочь."""
    keys = [db.list_snap_keep(CHAT, "admin", f"№{n}", _rows(1)) for n in range(listsnap.PER_CHAT + 1)]
    assert _raw(db, keys[0])["why"] == "evicted"
    clock.t = T0 + _dt.timedelta(days=91)
    got = db.list_snap_expire_due()
    assert keys[0] not in [k for k, _c in got], "такт назвал давно вытесненный снимок"
    assert _raw(db, keys[0]) is None, "отметка старше 90 дней осталась после такта"
    assert sorted(k for k, _c in got) == sorted(keys[1:]), "такт назвал не все истёкшие снимки"


# ── записи сообщений — не снимки ────────────────────────────────────────────

def _record(db, chat: int = CHAT, mid: int = 77) -> str:
    """Запись сообщения со «Скрыть» (без ссылок); вернуть её ключ после префикса."""
    db.list_msg_put(chat, mid, timeutil.now_iso(), "", HIDE_KB, [], None)
    assert db.list_msg_get(chat, mid), "запись сообщения не легла — тест ничего не проверит"
    return f"m:{chat}:{mid}"


def test_the_snapshot_listing_does_not_see_message_records(db, clock):
    key = db.list_snap_keep(CHAT, "admin", "x", _rows(1))
    _record(db)
    assert [k for k, _d in db._snaps()] == [key], f"среди снимков — запись сообщения: {db._snaps()}"


def test_sweeps_and_the_nightly_pass_leave_message_records_alone(db, clock):
    """У записи нет `at` и `exp`: прими её уборка за снимок — сожмёт в отметку
    (нечитаемый срок — протух), и «Скрыть» потеряет запись."""
    _record(db)
    before = db.list_msg_get(CHAT, 77)
    for n in range(listsnap.PER_CHAT + 1):
        db.list_snap_keep(CHAT, "admin", f"№{n}", _rows(1))
    clock.t = DEATH + _dt.timedelta(days=100)
    db.list_snap_keep(OTHER, "admin", "y", _rows(1))
    named = db.list_snap_expire_due()
    assert db.list_msg_get(CHAT, 77) == before, f"уборка задела запись сообщения: {db.list_msg_get(CHAT, 77)}"
    assert not [k for k, _c in named if ":" in k], f"такт назвал запись сообщения снимком: {named}"


def test_a_record_key_opened_as_a_link_is_no_snapshot_and_leaves_a_log_line(db, clock, caplog):
    """Ссылка list-snap-m:… (подделанная или битая) не показывает запись как снимок
    и не превращает её в отметку; в журнале — почему «нет»."""
    mk = _record(db)
    before = db.list_msg_get(CHAT, 77)
    with caplog.at_level(logging.INFO, logger="awgbot.db"):
        got = db.list_snap_get(mk, CHAT)
    assert got is None, f"ключ записи открылся как снимок: {got}"
    log = "\n".join(r.getMessage() for r in caplog.records)
    assert f"lists: ключ {mk} — не снимок (чат {CHAT})" in log, log
    assert db.list_msg_get(CHAT, 77) == before, "открытие по ключу записи её изменило"
