"""Применение журнала данных резерва у следующего: события держателя
(util/rsvjrn/apply.py) и пачки счётчиков другой стороны (util/rsvjrn/counts.py).

Следующий — основа в отъезде или процесс открытия у резерва — повторяет
решения держателя по его событиям и складывает дельты счётчиков, которые
намерила другая сторона. Цена ошибки: повтор пачки, применённый второй раз,
— трафик посчитан дважды, лимит срабатывает раньше; событие поверх строки из
снимка, упавшее на уникальности, — вся пачка отвергнута, и следующий
навсегда отстаёт от держателя; новая колонка держателя, которой у
следующего нет, роняет применение — пара разной версии перестаёт сходиться;
одно не вставшее событие откатывает пачку — остальные решения теряются;
сброс месяца, сброс периода и сложение пары переезда, повторённые иначе, чем
у держателя, — счётчики сторон расходятся, лимит срабатывает у одной; дельта
до сброса месяца, сложенная в счётчики после сброса, — новый месяц
начинается с чужого расхода; рукопожатие, затёртое более старым, — живое
устройство выглядит пропавшим.
"""
from __future__ import annotations

import sqlite3

import pytest

from awgbot.infra.db import Database
from awgbot.util.rsvjrn import apply as japply
from awgbot.util.rsvjrn import counts as jcounts
from tests import jrnequiv

pytestmark = pytest.mark.integration

_FUTURE = "2099-01-01T00:00:00+03:00"
_START = "2026-01-01T00:00:00+03:00"


def capture(db, on: bool = True) -> None:
    conn = db._connection()
    conn.execute("UPDATE jrn_meta SET capture = ? WHERE id = 1", (1 if on else 0,))
    conn.commit()


def evs(db, after: int = 0) -> list[japply.Event]:
    return japply.events_after(db._connection(), after)


def traffic(db, did) -> tuple:
    row = db._connection().execute(
        "SELECT traffic_rx_month, traffic_tx_month, traffic_rx_period, traffic_tx_period, rf_rx_month, rf_tx_month "
        "FROM device_traffic WHERE device_id = ?", (did,)).fetchone()
    return tuple(row) if row else None


class Pair:
    """Держатель (живая база теста) и следующий — копия его базы на момент
    `fork()`, открытая как Database в роли следующего (с запретом)."""

    def __init__(self, holder, tmp_path):
        self.holder = holder
        self.tmp = tmp_path
        self.follower = None

    def fork(self):
        path = str(self.tmp / "follower.db")
        dst = sqlite3.connect(path)
        try:
            self.holder._connection().backup(dst)
        finally:
            dst.close()
        f = Database(path)
        f.init_schema()
        f.jrn_pass(holder="peer", role="follower", why="тест: следование")
        self.follower = f
        return f

    def replay(self, after: int = 0) -> japply.Applied:
        return japply.apply(self.follower._connection(), evs(self.holder, after))

    def diff(self) -> list[str]:
        return jrnequiv.diff(self.holder._connection(), self.follower._connection())

    def meta(self) -> tuple:
        return tuple(self.follower._connection().execute(
            "SELECT applied, applying FROM jrn_meta WHERE id = 1").fetchone())


@pytest.fixture()
def pair(services, make_active_client, tmp_path):
    db = services.db
    ivan = make_active_client(name="Иван", tg_id=7100)
    dev = services.add_device(ivan.id, "Телефон")
    db.add_traffic_bulk([(dev.device_id, 1000, 500)])
    capture(db)
    with db._tx() as cur:
        cur.execute("DELETE FROM jrn_counts")              # дельты до начала пары — не наши (сторож мог включить захват раньше)
    p = Pair(db, tmp_path)
    p.svc, p.cid, p.did = services, ivan.id, dev.device_id
    yield p
    if p.follower is not None:
        p.follower.close()


def _decisions(p) -> None:
    """Разнообразные решения держателя: всех классов, I/U/D, каскад."""
    db = p.holder
    vera = db.create_client("Вера", 3, _START, _FUTURE, "INV-DUMMY")
    nb = db.create_device(vera, "Ноутбук", "DUMMY-pub-nb", "DUMMY-psk", "10.8.1.77")
    db.update_client_fields(p.cid, period_end=_FUTURE)
    db.update_device_fields(p.did, name="Планшет", traffic_limit=777)
    db.client_mask_apply(p.cid, or_bits=4)
    db.device_mask_apply(nb, or_bits=2)
    db.add_routing_domain(p.cid, "example.org")
    db.set_device_friend(p.did, friend_code="DUMMY-code", friend_status="pending")
    db.set_pause_balance(p.cid, 5)
    slot = db.gateway_add(nb, "awglink", 443, "10.99.99.0/30")
    db.gateway_update(slot.id, link_port=8443)
    from awgbot.util.rsvjrn import classes
    db.set_state(classes.STATE_DECISION[0], "DUMMY-value")
    db.delete_client(vera)                                  # каскадом — устройство, слот, счётчики


# ── события ─────────────────────────────────────────────────────────────────

def test_the_follower_replaying_the_events_has_the_holders_decisions(pair):
    """Главная гарантия: копия на момент передачи плюс события держателя — та же
    база решений, масок и данных хоста. Запрет следующего применению не мешает."""
    f = pair.fork()
    _decisions(pair)
    res = pair.replay()
    assert res.lost == [], res.lost
    assert pair.diff() == [], pair.diff()
    last = evs(pair.holder)[-1].i
    assert pair.meta() == (last, 0), f"applied не дошёл до последнего номера или отметка осталась: {pair.meta()}"
    mine = [e.i for e in evs(f)]
    assert mine == [e.i for e in evs(pair.holder)], "следующий записал события под своими номерами или эхом"


def test_a_repeated_batch_changes_nothing(pair):
    """Повтор пачки (переотправка неподтверждённого) — пропуск по номеру."""
    f = pair.fork()
    _decisions(pair)
    all_ev = evs(pair.holder)
    first = japply.apply(f._connection(), all_ev[:3])
    assert first.last == all_ev[2].i and len(first.done) == 3
    again = japply.apply(f._connection(), all_ev)
    assert again.skipped == 3 and len(again.done) == len(all_ev) - 3, again
    once_more = japply.apply(f._connection(), all_ev)
    assert once_more.skipped == len(all_ev) and once_more.done == [] and once_more.last == all_ev[-1].i
    assert pair.diff() == []
    assert pair.meta()[0] == all_ev[-1].i


def test_an_insert_over_an_existing_row_takes_the_events_values(pair):
    """Строку уже привёз снимок — вставка становится правкой, а не отказом."""
    f = pair.fork()
    ev = japply.Event(i=1, e=1, t="clients", op="I", k={"id": pair.cid}, c={"id": pair.cid, "name": "Иван Петров"})
    res = japply.apply(f._connection(), [ev])
    assert res.lost == [] and [e.i for e in res.done] == [1], res
    rows = f._connection().execute("SELECT name FROM clients WHERE id = ?", (pair.cid,)).fetchall()
    assert [r[0] for r in rows] == ["Иван Петров"], rows


def test_a_column_the_follower_does_not_have_is_dropped(pair):
    """Держатель новее: колонки, которой у следующего нет, — отбрасываем, прочее применяем."""
    f = pair.fork()
    ev = japply.Event(i=1, e=1, t="clients", op="U", k={"id": pair.cid},
                      c={"name": "Иван Н.", "novelty": "DUMMY"}, o={"name": "Иван", "novelty": None})
    res = japply.apply(f._connection(), [ev])
    assert res.lost == [], res.lost
    assert f.get_client(pair.cid).name == "Иван Н."


def test_a_missing_table_and_a_conflict_are_reported_and_the_rest_applies(pair):
    """Не вставшее (таблицы нет, уникальность) — в отчёт; остальные события
    пачки применены, applied дошёл до конца."""
    f = pair.fork()
    other_addr = f.get_device(pair.did).address
    batch = [
        japply.Event(i=1, e=1, t="brand_new_table", op="I", k={"id": 1}, c={"id": 1}),
        japply.Event(i=2, e=1, t="devices", op="I", k={"id": 900},
                     c={"id": 900, "client_id": pair.cid, "name": "Двойник адреса", "public_key": "DUMMY-pub-x",
                        "preshared_key": "DUMMY-psk", "address": other_addr, "created_at": _START}),
        japply.Event(i=3, e=1, t="clients", op="U", k={"id": pair.cid}, c={"name": "После конфликта"}),
    ]
    res = japply.apply(f._connection(), batch)
    lost = {ev.i: why for ev, why in res.lost}
    assert set(lost) == {1, 2}, res.lost
    assert "UNIQUE" in lost[2], lost[2]
    assert f.get_client(pair.cid).name == "После конфликта", "событие после не вставшего не применено"
    assert f.get_device(900) is None
    assert pair.meta() == (3, 0)


def test_a_broken_batch_rolls_back_whole_and_leaves_no_applying_mark(pair):
    """Сбой посреди пачки не уровня строки — откат целиком: ни половины решений,
    ни отметки applying, глушащей запрет."""
    f = pair.fork()
    batch = [japply.Event(i=1, e=1, t="clients", op="U", k={"id": pair.cid}, c={"name": "Наполовину"}),
             japply.Event(i=2, e=1, t="device_traffic", op="period", k={})]      # без client_id
    with pytest.raises(Exception):
        japply.apply(f._connection(), batch)
    assert f.get_client(pair.cid).name == "Иван", "половина пачки осталась"
    assert pair.meta() == (0, 0), pair.meta()


def test_a_parent_delete_cascades_on_the_follower(pair):
    """Удаление профиля у следующего уносит его устройства и счётчики каскадом,
    даже если события детей не пришли (держатель старшей схемы)."""
    f = pair.fork()
    res = japply.apply(f._connection(), [japply.Event(i=1, e=1, t="clients", op="D", k={"id": pair.cid})])
    assert res.lost == []
    assert f.get_device(pair.did) is None and traffic(f, pair.did) is None, "сироты остались после каскада"


def test_the_followers_own_triggers_stay_silent_while_applying(pair):
    """Следующий с включённым захватом не пишет эхо применённых решений своими номерами."""
    f = pair.fork()
    _decisions(pair)
    n = len(evs(pair.holder))
    pair.replay()
    assert len(evs(f)) == n


# ── операции над счётчиками ─────────────────────────────────────────────────

def test_month_period_and_pair_merge_give_the_holders_counters(pair):
    """Сброс месяца, сброс периода и сложение пары на завершении переезда:
    у следующего — те же счётчики, что у держателя, и та же отметка месяца."""
    db = pair.holder
    twin = pair.svc.add_device(pair.cid, "Двойник").device_id
    db.add_traffic_bulk([(twin, 70, 30)])
    db.rf_add_bulk([(pair.did, 9, 4)])
    f = pair.fork()
    db.merge_traffic(pair.did, twin)
    db.delete_device(pair.did)                             # финал переезда: старая строка уходит
    db.reset_month_traffic_all("2026-11")
    db.add_traffic_bulk([(twin, 5, 5)])                     # опрос после сброса — дельта, не событие
    res = pair.replay()
    assert res.lost == []
    f._connection().execute("UPDATE device_traffic SET traffic_rx_month = traffic_rx_month + 5, "
                            "traffic_tx_month = traffic_tx_month + 5, traffic_rx_period = traffic_rx_period + 5, "
                            "traffic_tx_period = traffic_tx_period + 5 WHERE device_id = ?", (twin,))
    f._connection().commit()                                # та же дельта после сброса — как пришла бы пачкой
    db.reset_period_traffic(pair.cid)
    assert pair.replay(res.last).lost == []
    assert traffic(f, twin) == traffic(db, twin), (traffic(f, twin), traffic(db, twin))
    assert f.get_state("last_monthly_reset") == "2026-11"
    assert pair.diff() == []


def test_the_pair_merge_before_the_delete_keeps_the_old_devices_traffic(pair):
    """Без сложения проигрывание удаления старой строки унесло бы её трафик."""
    db = pair.holder
    twin = pair.svc.add_device(pair.cid, "Двойник").device_id
    f = pair.fork()
    db.merge_traffic(pair.did, twin)
    db.delete_device(pair.did)
    pair.replay()
    assert traffic(f, twin)[:2] == (1000, 500), traffic(f, twin)


# ── пачки счётчиков ─────────────────────────────────────────────────────────

def _jrn_counts(db) -> list[tuple]:
    return [tuple(r) for r in db._connection().execute("SELECT s, month FROM jrn_counts ORDER BY s")]


def test_the_fold_sums_by_month_and_device_and_takes_the_latest_handshake(pair):
    db, did = pair.holder, pair.did
    other = pair.svc.add_device(pair.cid, "Ноутбук").device_id
    db.reset_month_traffic_all("2026-10")
    db.add_traffic_bulk([(did, 10, 1), (other, 3, 3)])
    db.rf_add_bulk([(did, 4, 2)])
    db.update_device_fields(did, last_handshake=1_700_000_500)
    db.update_device_fields(did, last_handshake=1_700_000_100)   # пришло позже, но старше
    db.add_traffic_bulk([(did, 20, 2)])
    db.reset_month_traffic_all("2026-11")
    db.add_traffic_bulk([(did, 7, 7)])
    b = jcounts.fold(db._connection(), "line:a")
    assert b.host == "line:a" and b.s1 == _jrn_counts(db)[-1][0] and b.s0 == 0
    assert b.months["2026-10"][did] == [30, 3, 4, 2, 1_700_000_500], b.months
    assert b.months["2026-10"][other] == [3, 3, 0, 0, None]
    assert b.months["2026-11"] == {did: [7, 7, 0, 0, None]}
    assert jcounts.Batch.from_json(b.as_json()) == b, "пачка не переживает упаковку"


def test_the_fold_after_a_number_and_drop_sent(pair):
    db, did = pair.holder, pair.did
    db.add_traffic_bulk([(did, 1, 1)])
    db.add_traffic_bulk([(did, 2, 2)])
    db.add_traffic_bulk([(did, 4, 4)])
    s = [r[0] for r in _jrn_counts(db)]
    tail = jcounts.fold(db._connection(), "h", after=s[0])
    assert sum(v[0] for m in tail.months.values() for v in m.values()) == 6, tail.months
    assert jcounts.fold(db._connection(), "h", after=s[-1]) is None, "после последнего номера слать нечего"
    with db._tx() as cur:
        jcounts.drop_sent(cur.connection, s[1])
    assert [r[0] for r in _jrn_counts(db)] == [s[2]], "подтверждённые строки остались или ушли лишние"
    assert jcounts.fold(db._connection(), "h").s1 == s[2]


def _batch(month, did, rx=0, tx=0, rf_rx=0, rf_tx=0, hs=None, s=1, host="line:b") -> jcounts.Batch:
    # s — номер пачки хоста (bn): по нему получатель отсекает повтор
    return jcounts.Batch(host=host, bn=s, months={month: {did: [rx, tx, rf_rx, rf_tx, hs]}})


def test_a_batch_adds_to_the_counters_once(pair):
    """Пачка текущего месяца — в месячные и периодные счётчики; повтор и
    старший номер — отказ, другая сторона — своя нумерация."""
    f = pair.fork()
    conn, did = f._connection(), pair.did
    conn.execute("INSERT INTO server_state (key, value) VALUES ('last_monthly_reset', '2026-11') "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
    conn.commit()
    start = traffic(f, did)
    assert jcounts.apply(conn, _batch("2026-11", did, 100, 50, 10, 5, s=5)) is True
    after = traffic(f, did)
    assert after == tuple(a + d for a, d in zip(start, (100, 50, 100, 50, 10, 5))), (start, after)
    assert jcounts.apply(conn, _batch("2026-11", did, 100, 50, s=5)) is False, "повтор пачки принят"
    assert jcounts.apply(conn, _batch("2026-11", did, 100, 50, s=3)) is False, "старая пачка принята"
    assert traffic(f, did) == after, "повтор посчитан дважды"
    assert jcounts.apply(conn, _batch("2026-11", did, 1, 1, s=1, host="other:a")) is True, \
        "номера другого хоста спутаны с этим"
    assert traffic(f, did)[0] == after[0] + 1


def test_the_receiver_ends_with_the_senders_counters(pair):
    """Сквозь поток: две базы с одинаковыми счётчиками, одна намерила опросом —
    после пачки у другой то же."""
    db, did = pair.holder, pair.did
    db.reset_month_traffic_all("2026-11")
    f = pair.fork()
    db.add_traffic_bulk([(did, 123, 45)])
    db.rf_add_bulk([(did, 6, 7)])
    db.update_device_fields(did, last_handshake=1_700_000_900)
    assert jcounts.apply(f._connection(), jcounts.fold(db._connection(), "line:a")) is True
    assert traffic(f, did) == traffic(db, did), (traffic(f, did), traffic(db, did))
    assert int(f.get_device(did).last_handshake) == 1_700_000_900


def _archive(db, did, month) -> tuple | None:
    row = db._connection().execute("SELECT rx, tx, rf_rx, rf_tx, client_id FROM traffic_monthly "
                                   "WHERE device_id = ? AND month = ?", (did, month)).fetchone()
    return tuple(row) if row else None


def test_a_delta_from_before_the_reset_goes_to_the_archive_of_its_month(pair):
    """Граница месяца: дельта, снятая до сброса, у получателя — в архив прошлого
    месяца, а не в счётчики нового; после сброса — в счётчики."""
    db, did = pair.holder, pair.did
    db.reset_month_traffic_all("2026-10")
    f = pair.fork()
    db.add_traffic_bulk([(did, 100, 40)])
    db.reset_month_traffic_all("2026-11")
    db.add_traffic_bulk([(did, 7, 3)])
    pair.replay()                                          # сброс месяца — событием
    month_before = traffic(f, did)[:2]
    assert jcounts.apply(f._connection(), jcounts.fold(db._connection(), "line:a")) is True
    assert _archive(f, did, "2026-10") == (100, 40, 0, 0, pair.cid), _archive(f, did, "2026-10")
    assert traffic(f, did)[:2] == (month_before[0] + 7, month_before[1] + 3), traffic(f, did)
    assert traffic(f, did)[:2] == traffic(db, did)[:2]


def test_a_delta_of_an_archived_month_adds_to_its_archive_row(pair):
    f = pair.fork()
    conn, did = f._connection(), pair.did
    conn.execute("INSERT INTO server_state (key, value) VALUES ('last_monthly_reset', '2026-11') "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
    conn.execute("INSERT INTO traffic_monthly (device_id, client_id, month, rx, tx, rf_rx, rf_tx, archived_at) "
                 "VALUES (?, ?, '2026-10', 1000, 500, 0, 0, '2026-11-01T00:00:00+05:00')", (did, pair.cid))
    conn.commit()
    assert jcounts.apply(conn, _batch("2026-10", did, 10, 5, 2, 1, s=1)) is True
    assert _archive(f, did, "2026-10") == (1010, 505, 2, 1, pair.cid)
    n = conn.execute("SELECT count(*) FROM traffic_monthly WHERE device_id = ? AND month = '2026-10'", (did,)).fetchone()[0]
    assert n == 1, "вторая строка архива того же месяца"


def test_a_delta_from_before_the_reset_still_counts_into_the_period(pair):
    """Периодные счётчики месячный сброс не трогает: у держателя дельта до
    сброса в периоде осталась — у получателя тоже должна, иначе лимит периода
    стороны считают по-разному."""
    db, did = pair.holder, pair.did
    db.reset_month_traffic_all("2026-10")
    f = pair.fork()
    db.add_traffic_bulk([(did, 100, 40)])
    db.reset_month_traffic_all("2026-11")
    pair.replay()
    jcounts.apply(f._connection(), jcounts.fold(db._connection(), "line:a"))
    assert traffic(f, did)[2:4] == traffic(db, did)[2:4], \
        f"период получателя {traffic(f, did)[2:4]} ≠ держателя {traffic(db, did)[2:4]}"


def test_a_batch_of_a_month_the_receiver_has_not_reset_into_yet_is_not_lost(pair):
    """Пачка, обогнавшая событие сброса месяца (держатель уже сбросил, следующий
    ещё нет): дельта нового месяца не должна пропасть — ни молча, ни после
    сброса, ни при переотправке."""
    db, did = pair.holder, pair.did
    db.reset_month_traffic_all("2026-10")
    f = pair.fork()
    mark = max(e.i for e in evs(db))
    db.reset_month_traffic_all("2026-11")
    db.add_traffic_bulk([(did, 9, 9)])
    b = jcounts.fold(db._connection(), "line:a")
    jcounts.apply(f._connection(), b)                       # пачка раньше события сброса
    pair.replay(mark)                                       # сброс доехал
    jcounts.apply(f._connection(), b)                       # переотправка неподтверждённой
    assert traffic(f, did)[:2] == traffic(db, did)[:2] == (9, 9), \
        f"дельта нового месяца потеряна: у получателя {traffic(f, did)[:2]}, у держателя {traffic(db, did)[:2]}"


def test_the_handshake_is_the_maximum_of_both_sides(pair):
    f = pair.fork()
    conn, did = f._connection(), pair.did
    f.update_device_fields(did, last_handshake=2_000_000_000)
    m = jcounts.current_month(conn)
    assert jcounts.apply(conn, _batch(m, did, hs=1_900_000_000, s=1)) is True
    assert int(f.get_device(did).last_handshake) == 2_000_000_000, "старое рукопожатие затёрло новое"
    assert jcounts.apply(conn, _batch(m, did, hs=2_100_000_000, s=2)) is True
    assert int(f.get_device(did).last_handshake) == 2_100_000_000


def test_the_receivers_month_is_its_last_reset_or_the_calendar(pair):
    from awgbot.util import timeutil
    f = pair.fork()
    conn = f._connection()
    conn.execute("DELETE FROM server_state WHERE key = 'last_monthly_reset'")
    conn.commit()
    assert jcounts.current_month(conn) == timeutil.now().strftime("%Y-%m")
    conn.execute("INSERT INTO server_state (key, value) VALUES ('last_monthly_reset', '2026-07')")
    conn.commit()
    assert jcounts.current_month(conn) == "2026-07"
