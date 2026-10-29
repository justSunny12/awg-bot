"""Записи сообщений `list_snap:m:<чат>:<номер>` (infra/db/listsnap/msgs) на
настоящей базе и замороженных часах: три причины держать запись (ссылки на
снимки, «Скрыть», итог), возраст «Скрыть», привязка снимка к сообщению и её
отказы, правка с ушедшей ссылкой, забвение и «править нечего», записи к
правке тактом, снятие записей с кнопками действия при передаче Telegram
резерву; разбор клавиатуры.

Цена ошибки: запись без причины — база копит по строке на каждое сообщение
бота; запись «Скрыть» без срока — кнопка висит дольше 48 часов, и нажатие
отвечает «сообщение не удалить»; снятая раньше суток — уведомление нельзя
убрать из чата; привязка чужого снимка — админ, вставивший в объявление
ссылку `list-snap-…` из другого чата, получит правку чужого сообщения тактом;
привязка уже показанного — два сообщения делят снимок, и «Скрыть» в одном
удаляет список у другого; неотпущенный ключ после правки — снимок нельзя
показать снова; забвение, оставившее отметку, — ссылка на удалённое
сообщение отвечает «больше не хранится» вместо «нет»; `final`, удаливший
отметки, — ссылки, которые уже не правятся, отвечают «списка нет», будто
подделаны; записи с кнопками действия у основы после переезда — она правит
меню, за которое отвечает резерв.
"""
from __future__ import annotations

import datetime as _dt
import json
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from awgbot.bot import keyboards as kb
from awgbot.core import config
from awgbot.infra.db import listsnap
from awgbot.infra.db.listsnap import expiry
from awgbot.util import timeutil

pytestmark = pytest.mark.integration

CHAT, OTHER = 5550001, 5550002
T0 = _dt.datetime(2026, 10, 27, 14, 5, 0, tzinfo=config.TZ)
DEATH = _dt.datetime(2026, 11, 27, 0, 0, 0, tzinfo=config.TZ)
MIN = 60
HOUR = 3600


def _kb(*rows: list[tuple[str, str]]) -> str:
    """Клавиатура JSON-строкой, как её хранит запись: ряды (подпись, колбэк)."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=c) for t, c in row]
                                                 for row in rows]).model_dump_json(exclude_none=True)


HIDE_KB = kb.hide_only().model_dump_json(exclude_none=True)                 # уведомление: одна «Скрыть»
EXTEND_KB = kb.append_hide_row(InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="⏱ Продлить", callback_data="ext:take:5")]])).model_dump_json(exclude_none=True)
MENU_KB = _kb([("👥 Пользователи", "m:users")], [("⚙️ Настройки", "m:settings")])   # живое меню, без «Скрыть»


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


def _rows(n: int) -> list[str]:
    return [f"<code>r{i:04d}.example</code> — не похоже на адрес" for i in range(n)]


def _snap(db, chat: int = CHAT, title: str = "⚠️ Не добавлено") -> str:
    return db.list_snap_keep(chat, "admin", title, _rows(3))


def _html(*keys: str) -> str:
    return "Итог" + "".join(f' <a href="https://t.me/test_bot?start=list-snap-{k}">и ещё 3</a>' for k in keys)


def _raw(db, key: str) -> dict | None:
    row = db._connection().execute("SELECT value FROM server_state WHERE key = ?",
                                   (listsnap.PREFIX + key,)).fetchone()
    return None if row is None else json.loads(row[0])


def _msg_keys(db) -> list[str]:
    return [r[0] for r in db._connection().execute(
        "SELECT key FROM server_state WHERE substr(key, 1, 12) = 'list_snap:m:'").fetchall()]


def _sent(at: _dt.datetime | None = None) -> str:
    return timeutil.to_iso(at or timeutil.now())


# ── разбор клавиатуры ───────────────────────────────────────────────────────

@pytest.mark.parametrize("kb_json, hide, action", [
    (HIDE_KB, True, False),
    (EXTEND_KB, True, True),
    (MENU_KB, False, True),
    ("", False, False),
    (None, False, False),
    ("{не json", False, False),
    (json.dumps({"inline_keyboard": [[{"text": "Скрыть", "callback_data": "hd"}]]}), True, False),
], ids=["hide-only", "extend+hide", "menu", "empty", "none", "broken", "hand-made"])
def test_the_keyboard_is_read_by_the_hide_callback(kb_json, hide, action):
    """«Скрыть» — по колбэку `hd` (что шлёт бот, `hide_only`), кнопки действия —
    всё прочее; пустая и битая клавиатура — ни того, ни другого."""
    assert (listsnap.kb_has_hide(kb_json), listsnap.kb_has_action(kb_json)) == (hide, action), kb_json


# ── три причины держать запись ──────────────────────────────────────────────

def test_a_notification_with_only_hide_and_no_links_has_a_record(db, clock):
    """Уведомление без ссылок, но со «Скрыть»: запись нужна — такт снимет кнопку через сутки."""
    assert db.list_msg_put(CHAT, 10, _sent(), "Уведомление", HIDE_KB, [], None) is False, \
        "без ссылок привязки нет — True обманет вызывающего"
    rec = db.list_msg_get(CHAT, 10)
    assert rec is not None, "запись уведомления со «Скрыть» не легла"
    assert (rec["chat"], rec["mid"], rec["kb"], rec["keys"], rec["html"], rec["note"]) == \
        (CHAT, 10, HIDE_KB, [], "", ""), rec
    assert rec["sent"] == _sent(T0), rec


def test_a_message_without_links_hide_or_note_leaves_no_record(db, clock):
    for kbj in (None, "", MENU_KB):
        assert db.list_msg_put(CHAT, 11, _sent(), "Текст", kbj, [], None) is False
        assert db.list_msg_get(CHAT, 11) is None and not _msg_keys(db), \
            f"запись без единой причины (kb={kbj!r}): {_msg_keys(db)}"


def test_hide_is_due_from_23_hours_30_minutes_not_a_minute_earlier(db, clock):
    """Такт снимает «Скрыть» с сообщений не моложе 23 ч 30 мин: в 23 ч 29 мин — рано."""
    db.list_msg_put(CHAT, 10, _sent(), "Уведомление", HIDE_KB, [], None)
    rec = db.list_msg_get(CHAT, 10)
    at_2329 = T0 + _dt.timedelta(hours=23, minutes=29)
    at_2330 = T0 + _dt.timedelta(hours=23, minutes=30)
    assert expiry.HIDE_KEEP_S == 23 * HOUR + 30 * MIN
    assert listsnap.hide_due(rec, at_2329) is False, "«Скрыть» снята раньше 23 ч 30 мин"
    assert listsnap.hide_due(rec, at_2330) is True, "«Скрыть» не снята в 23 ч 30 мин"
    assert db.list_msg_due(at_2329) == [], "запись к правке раньше срока «Скрыть»"
    due = db.list_msg_due(at_2330)
    assert [(r["mid"], r["hide"], r["dead"]) for r in due] == [(10, True, [])], due


def test_hide_age_is_counted_in_real_seconds_across_a_clock_change(monkeypatch):
    """Берлин, 25.10 часы отходят на час: отправлено 24.10 12:00, по стенным часам
    25.10 10:30 прошло уже 23 ч 30 мин — снять; 25.10 09:30 — 22 ч 30 мин, рано."""
    berlin = ZoneInfo("Europe/Berlin")
    monkeypatch.setattr(config, "TZ", berlin)
    monkeypatch.setattr(timeutil, "TZ", berlin)
    rec = {"sent": timeutil.to_iso(_dt.datetime(2026, 10, 24, 12, 0, tzinfo=berlin))}
    assert listsnap.hide_due(rec, _dt.datetime(2026, 10, 25, 10, 30, tzinfo=berlin)) is True, \
        "возраст «Скрыть» считан по стенным часам — через переход на час меньше"
    assert listsnap.hide_due(rec, _dt.datetime(2026, 10, 25, 9, 30, tzinfo=berlin)) is False


def test_hide_of_unknown_age_is_due(db, clock):
    """Нечитаемый `sent` — кнопка неизвестного возраста, снять."""
    assert listsnap.hide_due({"sent": "вчера"}, T0) is True


def test_hide_gone_from_the_keyboard_and_nothing_else_drops_the_record(db, clock):
    """Правка убрала «Скрыть» (такт снял, меню перерисовано без неё), ссылок и итога
    нет — запись прочь."""
    db.list_msg_put(CHAT, 10, _sent(), "Уведомление", HIDE_KB, [], None)
    db.list_msg_put(CHAT, 10, _sent(), "Уведомление", "", [], None)
    assert db.list_msg_get(CHAT, 10) is None and not _msg_keys(db), "запись осталась без причины"


def test_a_note_keeps_the_record_and_an_edit_without_it_drops_it(db, clock):
    """Живое меню с итогом первой строкой: запись держит итог (по нему меню станет
    следом); правка, не менявшая итог (None), его хранит; правка без итога ("") — снимает."""
    db.list_msg_put(CHAT, 20, _sent(), "✅ Готово\n\nМеню", MENU_KB, [], "✅ Готово")
    assert db.list_msg_get(CHAT, 20)["note"] == "✅ Готово", "итог не удержал запись меню"
    db.list_msg_put(CHAT, 20, _sent(), "✅ Готово\n\nМеню", None, [], None)
    rec = db.list_msg_get(CHAT, 20)
    assert rec and rec["note"] == "✅ Готово" and rec["kb"] == MENU_KB, \
        f"правка без kb/note (не менялись) потеряла их: {rec}"
    db.list_msg_put(CHAT, 20, _sent(), "Меню", MENU_KB, [], "")
    assert db.list_msg_get(CHAT, 20) is None, "правка без итога не сняла запись меню"


def test_the_sent_moment_is_the_first_one_not_the_edit(db, clock):
    """Правка не молодит сообщение: «Скрыть» считается от отправки."""
    db.list_msg_put(CHAT, 10, _sent(), "Уведомление", HIDE_KB, [], None)
    clock.advance(5 * HOUR)
    db.list_msg_put(CHAT, 10, _sent(), "Уведомление, правка", HIDE_KB, [], None)
    assert db.list_msg_get(CHAT, 10)["sent"] == _sent(T0), "правка сдвинула момент отправки"


# ── привязка снимка ─────────────────────────────────────────────────────────

def test_a_new_link_of_this_chat_binds_the_snapshot_to_the_message(db, clock):
    key = _snap(db)
    assert db.list_msg_put(CHAT, 30, _sent(), _html(key), HIDE_KB, [key], None) is True, \
        "новая привязка не названа — вытесненные не будут поправлены сразу"
    rec = db.list_msg_get(CHAT, 30)
    assert rec["keys"] == [key] and rec["html"] == _html(key), rec
    assert _raw(db, key)["msg"] == 30, f"снимок не знает своего сообщения: {_raw(db, key)}"


def test_put_says_true_only_when_a_new_link_got_bound(db, clock):
    """Повтор той же правки — False и ничего не портит; добавилась вторая ссылка — True."""
    a, b = _snap(db), _snap(db)
    assert db.list_msg_put(CHAT, 30, _sent(), _html(a), HIDE_KB, [a], None) is True
    assert db.list_msg_put(CHAT, 30, _sent(), _html(a), HIDE_KB, [a], None) is False, \
        "повтор той же правки назван новой привязкой"
    assert db.list_msg_get(CHAT, 30)["keys"] == [a] and _raw(db, a)["msg"] == 30, "повтор правки испортил привязку"
    assert db.list_msg_put(CHAT, 30, _sent(), _html(a, b), HIDE_KB, [a, b, b], None) is True
    assert db.list_msg_get(CHAT, 30)["keys"] == [a, b], "ключ задвоился или не добавился"


@pytest.mark.parametrize("case", ["foreign", "shown", "missing", "evicted", "expired"])
def test_a_link_that_must_not_bind_is_not_bound(db, clock, case):
    """Снимок другого чата, уже показанный другим сообщением, несуществующий,
    вытесненный, протухший: не привязан — False, ключей в записи нет, у чужого
    снимка `msg` не тронут."""
    if case == "foreign":
        key = _snap(db, OTHER)
    elif case == "shown":
        key = _snap(db)
        assert db.list_msg_put(CHAT, 29, _sent(), _html(key), HIDE_KB, [key], None)
    elif case == "missing":
        key = "zzzzzzzzzz"
    elif case == "evicted":
        key = _snap(db)
        for _ in range(listsnap.PER_CHAT):
            _snap(db)
        assert _raw(db, key)["why"] == "evicted"
    else:
        key = _snap(db)
        clock.t = DEATH
    before = _raw(db, key)
    assert db.list_msg_put(CHAT, 30, _sent(), _html(key), HIDE_KB, [key], None) is False, f"{case}: привязан"
    rec = db.list_msg_get(CHAT, 30)
    assert rec is not None and rec["keys"] == [] and rec["html"] == "", f"{case}: ключ в записи: {rec}"
    assert _raw(db, key) == before, f"{case}: снимок изменён попыткой привязки: {_raw(db, key)}"


def test_a_link_that_did_not_bind_and_no_other_reason_leaves_no_record(db, clock):
    key = _snap(db, OTHER)
    assert db.list_msg_put(CHAT, 30, _sent(), _html(key), None, [key], None) is False
    assert db.list_msg_get(CHAT, 30) is None, "запись ради чужой ссылки"


# ── правка: ушедшая ссылка ──────────────────────────────────────────────────

def test_an_edit_that_drops_the_link_releases_the_snapshot_which_still_opens(db, clock):
    """Ссылка ушла из текста: снимок отпущен (`msg` = 0), но открывается — на нём мог
    стоять открытый экран «весь список»; запись держится «Скрыть», без ключей."""
    key = _snap(db)
    db.list_msg_put(CHAT, 30, _sent(), _html(key), HIDE_KB, [key], None)
    assert db.list_msg_put(CHAT, 30, _sent(), "Итог", HIDE_KB, [], None) is False
    assert _raw(db, key)["msg"] == 0, f"ушедший ключ не отпущен: {_raw(db, key)}"
    assert db.list_snap_get(key, CHAT)["rows"] == _rows(3), "отпущенный снимок не открывается"
    rec = db.list_msg_get(CHAT, 30)
    assert rec["keys"] == [] and rec["html"] == "", rec


def test_a_released_snapshot_can_be_shown_by_another_message(db, clock):
    key = _snap(db)
    db.list_msg_put(CHAT, 30, _sent(), _html(key), HIDE_KB, [key], None)
    db.list_msg_put(CHAT, 30, _sent(), "Итог", HIDE_KB, [], None)
    assert db.list_msg_put(CHAT, 31, _sent(), _html(key), HIDE_KB, [key], None) is True
    assert _raw(db, key)["msg"] == 31


def test_an_edit_that_swaps_links_releases_the_old_and_binds_the_new(db, clock):
    a, b = _snap(db), _snap(db)
    db.list_msg_put(CHAT, 30, _sent(), _html(a), HIDE_KB, [a], None)
    assert db.list_msg_put(CHAT, 30, _sent(), _html(b), HIDE_KB, [b], None) is True
    assert (_raw(db, a)["msg"], _raw(db, b)["msg"]) == (0, 30)
    assert db.list_msg_get(CHAT, 30)["keys"] == [b]


def test_releasing_does_not_steal_a_snapshot_bound_to_another_message(db, clock):
    """Сообщение 30 «ушло» от ключа, которым владеет сообщение 31, — у 31 привязка цела."""
    a = _snap(db)
    db.list_msg_put(CHAT, 31, _sent(), _html(a), HIDE_KB, [a], None)
    db.list_msg_put(CHAT, 30, _sent(), _html(a), HIDE_KB, [a], None)          # не привязан: уже показан
    db.list_msg_put(CHAT, 30, _sent(), "Итог", HIDE_KB, [], None)
    assert _raw(db, a)["msg"] == 31, "правка чужого сообщения отпустила снимок"


# ── забвение и «править нечего» ─────────────────────────────────────────────

def test_forget_with_snapshots_drops_record_and_snapshots_without_marks(db, clock):
    """«Скрыть» нажата или сообщение удалено: запись и его снимки прочь, отметок
    нет — ссылки больше нигде нет, «больше не хранится» говорить некому."""
    a, b = _snap(db), _snap(db)
    keep = _snap(db)
    db.list_msg_put(CHAT, 30, _sent(), _html(a, b), HIDE_KB, [a, b], None)
    assert sorted(db.list_msg_forget(CHAT, 30, drop_snaps=True)) == sorted([a, b])
    assert db.list_msg_get(CHAT, 30) is None and not _msg_keys(db)
    assert _raw(db, a) is None and _raw(db, b) is None, "снимки удалённого сообщения остались (или отметками)"
    assert db.list_snap_get(a, CHAT) is None
    assert db.list_snap_get(keep, CHAT)["rows"] == _rows(3), "забвение задело чужой снимок"


def test_forget_without_snapshots_keeps_them(db, clock):
    a = _snap(db)
    db.list_msg_put(CHAT, 30, _sent(), _html(a), HIDE_KB, [a], None)
    db.list_msg_forget(CHAT, 30, drop_snaps=False)
    assert db.list_msg_get(CHAT, 30) is None
    assert db.list_snap_get(a, CHAT)["rows"] == _rows(3), "снимок удалён без drop_snaps"


def test_forget_of_an_unknown_message_is_harmless(db, clock):
    a = _snap(db)
    assert db.list_msg_forget(CHAT, 99, drop_snaps=True) == []
    assert db.list_snap_get(a, CHAT)["rows"] == _rows(3)


def test_final_drops_the_record_and_keeps_the_marks(db, clock):
    """Сообщение больше не править (403, прочие 400): запись прочь, отметка
    протухшего снимка остаётся — ссылка отвечает «больше не хранится»."""
    a = _snap(db)
    db.list_msg_put(CHAT, 30, _sent(), _html(a), HIDE_KB, [a], None)
    clock.t = DEATH
    db.list_snap_expire_due()
    db.list_msg_final(CHAT, 30)
    assert db.list_msg_get(CHAT, 30) is None and not _msg_keys(db), "запись осталась после final"
    assert db.list_snap_get(a, CHAT) == {"stale": True, "at": timeutil.to_iso(T0), "why": ""}, \
        "final убрал отметку — ссылка ответит «списка нет»"
    db.list_msg_final(CHAT, 30)                                                # повтор — без последствий


# ── записи к правке ─────────────────────────────────────────────────────────

def test_due_names_dead_keys_and_old_hide_and_skips_the_healthy(db, clock):
    """Мёртвый ключ — снимок протух (ещё не сжат или уже отметка), вытеснен или
    удалён; «Скрыть» — по возрасту. Здоровая запись в список не попадает."""
    expired = _snap(db)                                        # 27.10 — умрёт 27.11
    db.list_msg_put(CHAT, 40, _sent(), _html(expired), HIDE_KB, [expired], None)
    clock.advance(86400)                                       # 28.10
    gone = _snap(db)
    db.list_msg_put(CHAT, 41, _sent(), _html(gone), MENU_KB, [gone], None)
    with db._tx() as c:                                        # снимка нет совсем (база восстановлена и т. п.)
        c.execute("DELETE FROM server_state WHERE key = ?", (listsnap.PREFIX + gone,))
        db._tx_note(db._TX_COUNTERS)
    healthy = _snap(db)
    db.list_msg_put(CHAT, 42, _sent(), _html(healthy), MENU_KB, [healthy], None)
    db.list_msg_put(OTHER, 43, _sent(), "Уведомление", HIDE_KB, [], None)
    now = DEATH
    due = {(r["chat"], r["mid"]): (r["dead"], r["hide"]) for r in db.list_msg_due(now)}
    assert due == {(CHAT, 40): ([expired], True), (CHAT, 41): ([gone], False), (OTHER, 43): ([], True)}, due
    db.list_snap_expire_due(now)                               # такт сжал — ключ всё ещё мёртв
    mine = {r["mid"]: r["dead"] for r in db.list_msg_due(now, chat=CHAT)}
    assert mine == {40: [expired], 41: [gone]}, f"фильтр по чату пропустил чужое или отметка ожила: {mine}"
    assert {r["mid"] for r in db.list_msg_due(now, chat=OTHER)} == {43}


def test_an_evicted_key_is_dead(db, clock):
    a = _snap(db)
    db.list_msg_put(CHAT, 40, _sent(), _html(a), MENU_KB, [a], None)
    for _ in range(listsnap.PER_CHAT):
        _snap(db)
    due = db.list_msg_due()
    assert [(r["mid"], r["dead"], r["hide"]) for r in due] == [(40, [a], False)], due


def test_due_keeps_the_fields_needed_to_edit(db, clock):
    """Правка идёт по записи: текст и клавиатура — в той же записи, что мёртвый ключ."""
    a = _snap(db)
    db.list_msg_put(CHAT, 40, _sent(), _html(a), EXTEND_KB, [a], None)
    (rec,) = db.list_msg_due(DEATH)
    assert (rec["html"], rec["kb"], rec["keys"]) == (_html(a), EXTEND_KB, [a]), rec


# ── передача Telegram резерву ───────────────────────────────────────────────

def test_drop_live_removes_records_with_action_buttons_and_keeps_lone_hide(db, clock):
    """Право отвечать ушло резерву: записи «⏱ Продлить» + «Скрыть» и живого меню —
    прочь (их правит резерв), записи с одной «Скрыть» — остаются; снимки — тоже."""
    a = _snap(db)
    db.list_msg_put(CHAT, 50, _sent(), "Уведомление", HIDE_KB, [], None)
    db.list_msg_put(CHAT, 51, _sent(), "Срок истекает", EXTEND_KB, [], None)
    db.list_msg_put(CHAT, 52, _sent(), "✅ Готово\n\nМеню", MENU_KB, [], "✅ Готово")
    db.list_msg_put(OTHER, 53, _sent(), _html(), EXTEND_KB, [], None)
    db.list_msg_put(CHAT, 54, _sent(), _html(a), HIDE_KB, [a], None)
    assert db.list_msg_drop_live() == 3
    left = sorted((r["chat"], r["mid"]) for r in db.list_msg_all())
    assert left == [(CHAT, 50), (CHAT, 54)], f"после передачи остались не те записи: {left}"
    assert db.list_snap_get(a, CHAT)["rows"] == _rows(3), "снимок удалён вместе с записями"
    assert db.list_msg_drop_live() == 0, "повтор снял ещё что-то"
