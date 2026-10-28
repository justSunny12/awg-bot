"""Основа в следовании и бот основы на резерве (гость) с журналом данных — на
трёх базах: основа, резерв, гость (journal/follow, guestj, handover, route).

- основа в отъезде применяет события держателя (через бот резерва): пиры по
  строкам базы после применения, блокировки, сводка эпохи для Т-РУ5б (смена
  непустого Telegram, новое устройство у профиля админа);
- гость: свой журнал данных на диске до отправки, подтверждения основы,
  пачки клона (b) основе, счётчики оставшихся на основе сессий (a) себе;
- переезд кнопкой: гость следует за основой до `released{epoch, last}`,
  догоняет по `jneed` из хранилища резерва и берёт эпоху — без перезапуска;
- возврат: гость дождался подтверждений, ушёл в следование, `released`
  через резерв, основа берёт эпоху без слияния и перезапуска — база основы
  после возврата равна базе гостя по классам данных.

Мир — tests/integration/_reserve_jrn_duo.py. Ключи — заглушки.

Цена ошибки: основа в отъезде без пиров по событиям держателя — устройство,
заведённое на резерве, после возврата не подключается; без блокировок —
оставшиеся на основе сессии заблокированного продолжают ходить; сводка без
строк сверки — админ не узнаёт, что на резерве сменили Telegram профиля
или завели устройство на его профиль; гость, взявший эпоху до догона, —
решения основы последних секунд перед передачей пропадают; гость, не
взявший её, — люди на резерве не могут ничего изменить; возврат со
слиянием поверх применённых событий — счётчики дважды, а без догона —
правки последних секунд у резерва теряются.
"""
from __future__ import annotations

import sqlite3

import pytest

from awgbot.core import config
from awgbot.infra import awg
from awgbot.util.rsvjrn import counts as jcounts
from awgbot.util.rsvjrn import event
from tests import jrnequiv

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_jrn_duo import Duo, checked, sends, to_guest

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv

_START, _FUTURE = "2026-01-01T00:00:00+03:00", "2099-01-01T00:00:00+03:00"


@pytest.fixture()
def duo(mv, tmp_path, monkeypatch, fake_awg):
    monkeypatch.setattr(awg, "blocked_ips", lambda: set(fake_awg.blocked), raising=False)
    d = Duo(mv, tmp_path, monkeypatch)
    d.fake = fake_awg
    yield d
    d.close()


def _ivan(duo) -> tuple[int, int, str]:
    """Журнал включён, Иван с устройством у основы — до переезда (в журнале резерва)."""
    duo.journal_on()
    cid = duo.edit("Иван")
    duo.a.db.update_client_fields(cid, tg_id=7100)
    dev = [d for d in duo.a.db.list_all_devices() if d.client_id == cid][0]
    duo.tick()
    return cid, dev.id, dev.address


def _away(duo, *, button: bool = False):
    """Пользователи и бот у резерва: гость открыт по журналу, основа следует."""
    res = duo.open_guest(button=button)
    if not button:
        rm.go_away(duo.a)
        duo.a.reserve_epoch_sync()
        assert duo.a.reserve_following()
    return res


def _guest_round(duo) -> tuple[list, list]:
    """Такт гостя → бот резерва → основа → её ответы → бот резерва → гостю."""
    with duo.as_guest():
        msgs = duo.g.guest_jrn_tick()
    at_a = duo.b_to_a(duo.guest_to_b(msgs))
    back = duo.b_to_guest(duo.a_to_b(at_a))
    return msgs, at_a + back


def _diff(duo) -> list[str]:
    return jrnequiv.diff(duo.a.db._connection(), duo.gdb._connection())


# ── следование: события держателя ───────────────────────────────────────────

def test_the_primary_applies_the_holders_events_with_peers_blocks_and_the_report(duo):
    """Гость завёл устройство и заблокировал устройство Ивана — у основы та же
    база, пир нового устройства добавлен, DROP старому наложен, сводка эпохи
    считает устройство и пир."""
    cid, did, addr = _ivan(duo)
    _away(duo)
    assert duo.a.db.jrn_meta()["epoch"] == duo.gdb.jrn_meta()["epoch"], "основа следует не за эпохой гостя"
    with duo.as_guest():
        duo.g.db.create_device(cid, "Планшет", "DUMMY-pub-tab", "DUMMY-psk", "10.8.1.201", private_key="DUMMY-priv")
        duo.g.db.update_device_fields(did, block_reason=1)
    msgs, _ = _guest_round(duo)
    assert [k for k, _ in msgs] == ["jev"], msgs
    assert duo.fake.peers.get("DUMMY-pub-tab") == ("DUMMY-psk", "10.8.1.201"), \
        "устройство с резерва без пира у основы — после возврата не подключится"
    assert addr in duo.fake.blocked, "блокировка держателя не наложена оставшимся у основы сессиям"
    rep = duo.a.rsv_get("rsv_out")["jrn_report"]
    assert rep["devices"] == 1, rep
    assert _diff(duo) == [], "база основы разошлась с базой гостя"
    with duo.as_guest():
        assert duo.g.guest_jrn_drained(), "jack основы не дошёл до гостя"
    assert duo.a.db.jrn_meta()["applied"] == duo.gdb.jrn_last_i()


def test_a_deleted_device_removes_its_peer(duo):
    cid, did, _addr = _ivan(duo)
    _away(duo)
    pub = duo.a.db.get_device(did).public_key
    duo.fake.peers[pub] = ("DUMMY-psk", "10.8.1.9")
    with duo.as_guest():
        duo.g.db.delete_device(did)
    _guest_round(duo)
    assert duo.a.db.get_device(did) is None
    assert pub not in duo.fake.peers, "пир удалённого на резерве устройства остался у основы"
    assert duo.a.rsv_get("rsv_out")["jrn_report"]["peers_removed"] == 1


def test_the_report_has_the_check_lines_for_a_changed_telegram_and_an_admin_device(duo, make_active_client):
    """Смена непустого Telegram профиля и новое устройство у профиля админа —
    строки сверки в сводке; правка без смены Telegram строки не даёт."""
    cid, _did, _addr = _ivan(duo)
    admin = make_active_client(name="Админ", tg_id=int(config.ADMIN_ID))
    duo.tick()
    _away(duo)
    with duo.as_guest():
        duo.g.db.update_client_fields(cid, tg_id=7999)
        duo.g.db.update_client_fields(cid, name="Иван Петров")
        duo.g.db.create_device(admin.id, "Чужой", "DUMMY-pub-adm", "DUMMY-psk", "10.8.1.202", private_key="DUMMY-priv")
    _guest_round(duo)
    lost = duo.a.rsv_get("rsv_out")["jrn_report"]["lost"]
    assert len(lost) == 2, lost
    assert "сменён Telegram-аккаунт" in lost[0] and "7100 → 7999" in lost[0] and "Иван Петров" in lost[0], lost[0]
    assert "У профиля админа появилось устройство «Чужой»" in lost[1], lost[1]


def test_a_first_telegram_on_a_new_profile_is_not_a_check_line(duo):
    """Telegram проставлен впервые (прежний пуст) — это активация, не подмена."""
    _ivan(duo)
    _away(duo)
    with duo.as_guest():
        c = duo.g.db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V")
        duo.g.db.update_client_fields(c, tg_id=7300)
    _guest_round(duo)
    rep = duo.a.rsv_get("rsv_out")["jrn_report"]
    assert rep["lost"] == [] and rep["clients"] == 1, rep


def _guest_jev(duo, edit) -> dict:
    with duo.as_guest():
        edit(duo.g.db)
        (kind, body), = [m for m in duo.g.guest_jrn_tick() if m[0] == "jev"]
    return checked(kind, body)


def test_a_repeated_batch_is_not_applied_twice(duo):
    _ivan(duo)
    _away(duo)
    body = _guest_jev(duo, lambda db: db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V"))
    first = duo.a.reserve_jrn_in("jev", body)
    n = len(duo.a.db.list_clients())
    again = duo.a.reserve_jrn_in("jev", body)
    assert len(duo.a.db.list_clients()) == n, "повтор пачки применён второй раз"
    assert sends(again, "jack") == sends(first, "jack"), "на повтор нет того же подтверждения"
    assert duo.a.rsv_get("rsv_out")["jrn_report"]["clients"] == 1, "повтор посчитан в сводке"


def test_a_gap_asks_from_the_applied_and_applies_nothing(duo):
    _ivan(duo)
    _away(duo)
    applied = duo.a.db.jrn_meta()["applied"]
    _guest_jev(duo, lambda db: db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V"))     # потерялось
    body = _guest_jev(duo, lambda db: db.create_client("Ольга", 1, _START, _FUTURE, "INV-DUMMY-O"))
    acts = duo.a.reserve_jrn_in("jev", body)
    assert sends(acts) == [("jneed", {"line": duo.line, "from": applied + 1})], acts
    assert duo.a.db.jrn_meta()["applied"] == applied
    assert not [c for c in duo.a.db.list_clients() if c.name == "Ольга"], "события после дыры применены"


def test_the_follower_takes_the_epoch_of_the_holder_even_a_smaller_one(duo):
    """Держатель один: следующий принимает его эпоху как есть — и меньшую своей
    (смена роли без событий подняла мою), без jstale и без потери правок."""
    _ivan(duo)
    _away(duo)
    body = _guest_jev(duo, lambda db: db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V"))
    duo.a.db.jrn_adopt_epoch(body["e"] + 2)
    acts = duo.a.reserve_jrn_in("jev", body)
    assert [k for k, _ in sends(acts)] == ["jack"], acts
    assert [c for c in duo.a.db.list_clients() if c.name == "Вера"], "правка держателя меньшей эпохи потеряна"
    assert duo.a.db.jrn_meta()["epoch"] == body["e"], "эпоха держателя не принята"


def test_a_holder_answers_jstale_to_events_of_a_smaller_epoch(duo):
    """Эпоха здесь (основа держит), пришли события меньшей эпохи — jstale: отправитель
    уходит в следование."""
    _ivan(duo)
    duo.open_guest(button=False)
    body = _guest_jev(duo, lambda db: db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V"))
    duo.a.db.jrn_pass(holder="self", role="holder", why="тест", epoch=body["e"] + 3)
    e = duo.a.db.jrn_meta()["epoch"]
    assert sends(duo.a.reserve_jrn_in("jev", body)) == [("jstale", {"line": duo.line, "e": e})]
    assert not [c for c in duo.a.db.list_clients() if c.name == "Вера"]


def test_a_permuted_or_holey_batch_is_asked_again(duo):
    """Внутри пачки номера не подряд с applied + 1 (перестановка или пропуск) — не
    применять, jneed."""
    _ivan(duo)
    _away(duo)
    body = _guest_jev(duo, lambda db: [db.create_client(n, 1, _START, _FUTURE, f"INV-DUMMY-{n}") for n in ("Вера", "Ольга")])
    recs = event.split(bytes.fromhex(body["blob"]))
    applied = duo.a.db.jrn_meta()["applied"]
    for bad in (list(reversed(recs)), [recs[0]] + recs[2:]):
        b = dict(body, blob=b"".join(bad).hex(), n=len(bad))
        acts = duo.a.reserve_jrn_in("jev", b)
        assert sends(acts) == [("jneed", {"line": duo.line, "from": applied + 1})], acts
        assert duo.a.db.jrn_meta()["applied"] == applied, "пачка не подряд применена"


def test_going_follower_sets_applied_to_the_fork_and_the_first_jev_needs_no_jneed(duo):
    """Уход в следование: применённое — точка расхождения (точка гостя из st резерва);
    первое же событие держателя применяется без досылки."""
    _ivan(duo)
    duo.open_guest(button=False)
    fork = duo.gdb.jrn_meta()["fork_i"]
    duo.b.__dict__["_rsv_guest_jrn"] = {"e": duo.gdb.jrn_meta()["epoch"], "fork": fork}
    duo.st_b(**{"in": "serving"})
    rm.go_away(duo.a)
    duo.a.reserve_epoch_sync()
    m = duo.a.db.jrn_meta()
    assert (m["role"], m["applied"], m["fork_i"], m["epoch"]) == ("follower", fork, fork, duo.gdb.jrn_meta()["epoch"]), m
    body = _guest_jev(duo, lambda db: db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V"))
    assert [k for k, _ in sends(duo.a.reserve_jrn_in("jev", body))] == ["jack"]


def test_a_block_of_a_device_does_not_drop_and_readd_its_peer(duo, monkeypatch):
    """Правка устройства, не меняющая ключ, адрес, интерфейс и PSK (блокировка,
    имя), — пир не снимается и не ставится заново: снятие пира рвёт сессию
    человека у основы."""
    cid, did, _addr = _ivan(duo)
    _away(duo)
    removed: list[str] = []
    orig = awg.remove_peer
    monkeypatch.setattr(awg, "remove_peer", lambda pub, iface=None: (removed.append(pub), orig(pub, iface))[1])
    with duo.as_guest():
        duo.g.db.update_device_fields(did, block_reason=1)
        duo.g.db.update_device_fields(did, name="Телефон Ивана")
    _guest_round(duo)
    assert removed == [], f"пир снят и поставлен заново на правке без смены ключа: {removed}"
    assert duo.a.rsv_get("rsv_out")["jrn_report"]["peers_added"] == 0


def test_a_holder_does_not_apply_events_of_its_own_line(duo):
    """Основа держит эпоху (не следует) — пришедшие события своей линии не
    применяются и не подтверждаются: эпоха могла уйти сюда."""
    _ivan(duo)
    duo.open_guest(button=False)
    assert not duo.a.reserve_following()
    body = _guest_jev(duo, lambda db: db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V"))
    assert duo.a.reserve_jrn_in("jev", body) == []
    assert not [c for c in duo.a.db.list_clients() if c.name == "Вера"]


def test_a_record_under_another_key_is_asked_again_not_applied(duo):
    import os
    _ivan(duo)
    _away(duo)
    with duo.as_guest():
        duo.g.db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V")
        evs = duo.g.db.jrn_events_after(duo.gdb.jrn_meta()["fork_i"])
    bad = {"line": duo.line, "e": evs[0].e, "from": evs[0].i, "n": len(evs),
           "blob": b"".join(event.seal_event(os.urandom(32), duo.line, ev) for ev in evs).hex()}
    acts = duo.a.reserve_jrn_in("jev", bad)
    assert sends(acts, "jneed"), acts
    assert not [c for c in duo.a.db.list_clients() if c.name == "Вера"]


# ── счётчики клона и оставшихся сессий ──────────────────────────────────────

def test_the_clone_counts_reach_the_primary_once_and_add_to_the_traffic_out(duo):
    """Пачка клона (b) складывается у основы один раз, расход через резерв —
    в итог; jcack — гостю, его дельты сняты."""
    _cid, did, _addr = _ivan(duo)
    _away(duo)
    before = duo.a.db._connection().execute(
        "SELECT traffic_rx_month, traffic_tx_month FROM device_traffic WHERE device_id = ?", (did,)).fetchone()
    with duo.as_guest():
        duo.g.db.add_traffic_bulk([(did, 300, 400)])
        duo.g.db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V")      # пачка уходит с событиями
        msgs = duo.g.guest_jrn_tick()
    assert [k for k, _ in msgs] == ["jev", "jcnt"], msgs
    jcnt = dict(msgs)["jcnt"]
    at_a = duo.b_to_a(duo.guest_to_b(msgs))
    after = duo.a.db._connection().execute(
        "SELECT traffic_rx_month, traffic_tx_month FROM device_traffic WHERE device_id = ?", (did,)).fetchone()
    assert (after[0] - before[0], after[1] - before[1]) == (300, 400), (before, after)
    out = duo.a.rsv_get("rsv_traffic").get("out") or {}
    assert (out.get("rx"), out.get("tx")) == (300, 400), f"расход через резерв не учтён: {out}"
    assert ("jcack", {"line": duo.line, "side": "b", "s": jcnt["s"]}) in sends(at_a)
    again = duo.a.reserve_jrn_in("jcnt", jcnt)
    assert duo.a.db._connection().execute("SELECT traffic_rx_month FROM device_traffic WHERE device_id = ?",
                                          (did,)).fetchone()[0] == after[0], "повтор пачки клона сложен дважды"
    assert ((duo.a.rsv_get("rsv_traffic").get("out") or {}).get("rx")) == 300, "повтор пачки добавил расход через резерв"
    assert sends(again, "jcack"), "на повтор нет подтверждения"
    duo.b_to_guest(duo.a_to_b(at_a))
    assert duo.gdb._connection().execute("SELECT COUNT(*) FROM jrn_counts").fetchone()[0] == 0, \
        "подтверждённые дельты клона остались у гостя"


def test_a_clone_batch_of_a_past_month_goes_to_the_archive(duo):
    _cid, did, _addr = _ivan(duo)
    _away(duo)
    month = jcounts.current_month(duo.a.db._connection())
    y, m = int(month[:4]), int(month[5:])
    prev = f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
    batch = jcounts.Batch(host=f"{duo.line}:b", bn=5, months={prev: {did: [10, 20, 0, 0, None]}})
    raw = event.seal_counts(duo.a.reserve_k(), duo.line, "b", batch)
    duo.a.reserve_jrn_in("jcnt", checked("jcnt", {"line": duo.line, "side": "b", "s": 5, "blob": raw.hex()}))
    row = duo.a.db._connection().execute("SELECT rx, tx FROM traffic_monthly WHERE device_id = ? AND month = ?",
                                         (did, prev)).fetchone()
    assert row is not None and tuple(row) == (10, 20), f"дельта прошлого месяца не в архиве: {row}"


def test_the_primary_does_not_take_a_batch_of_its_own_side(duo):
    _cid, did, _addr = _ivan(duo)
    _away(duo)
    raw = event.seal_counts(duo.a.reserve_k(), duo.line, "a",
                            jcounts.Batch(host=f"{duo.line}:a", bn=1, months={jcounts.current_month(duo.a.db._connection()): {did: [1, 1, 0, 0, None]}}))
    assert duo.a.reserve_jrn_in("jcnt", {"line": duo.line, "side": "a", "s": 1, "blob": raw.hex()}) == []


def test_the_guest_adds_the_counts_of_the_sessions_left_at_the_primary(duo):
    """Основа в отъезде шлёт пачки своей стороны (a) — гость их складывает
    (лимит по сумме), резерв хранит и пересылает."""
    _cid, did, _addr = _ivan(duo)
    _away(duo)
    duo.a.db.add_traffic_bulk([(did, 70, 80)])
    duo.a.reserve_jrn_tick()
    duo.clock.advance(43 * 60)
    acts = duo.a.reserve_jrn_tick()
    assert [k for k, _ in sends(acts)] == ["jcnt"], acts
    before = duo.gdb._connection().execute("SELECT traffic_rx_month FROM device_traffic WHERE device_id = ?",
                                           (did,)).fetchone()[0]
    at_b = duo.a_to_b(acts)
    assert to_guest(at_b, "jcnt"), "пачка основы не ушла гостю"
    duo.b_to_guest(at_b)
    after = duo.gdb._connection().execute("SELECT traffic_rx_month FROM device_traffic WHERE device_id = ?",
                                          (did,)).fetchone()[0]
    assert after - before == 70, f"гость не сложил счётчики оставшихся у основы сессий: {before} → {after}"


# ── журнал данных гостя ─────────────────────────────────────────────────────

def test_the_guest_writes_its_journal_to_disk_before_sending(duo):
    """Записи гостя на диске до отправки (вместо rep.bin): пропавшее сообщение
    не теряет правку, повтор — с подтверждённого после RESEND_S."""
    _ivan(duo)
    _away(duo)
    with duo.as_guest():
        duo.g.db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V")
        (kind, body), = duo.g.guest_jrn_tick()                # пропало
        store = duo.g.guest_jrn_store()
        assert [event.head(r).n for r in store.events()] == list(range(body["from"], body["from"] + body["n"])), \
            "журнал гостя не записан до отправки"
        assert not duo.g.guest_jrn_drained()
        assert duo.g.guest_jrn_tick() == [], "повтор раньше срока"
        duo.clock.advance(25)
        again = duo.g.guest_jrn_tick()
        assert again and again[0][1]["from"] == body["from"], again
        assert len(store.events()) == body["n"], "повтор записан в журнал гостя второй раз"
        assert not duo.g.guest_snapshot_tick(force=True), "с журналом гость пишет rep.bin"


def test_a_jstale_from_the_primary_makes_the_guest_follow(duo):
    _ivan(duo)
    _away(duo)
    with duo.as_guest():
        e = duo.gdb.jrn_meta()["epoch"]
        duo.g.guest_jrn_in("jstale", {"line": duo.line, "e": e + 1})
        m = duo.gdb.jrn_meta()
        assert (m["epoch"], m["holder"], m["role"]) == (e + 1, "peer", "follower"), m
        with pytest.raises(sqlite3.IntegrityError, match="^jrn:"):
            duo.g.db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V")


# ── переезд кнопкой ─────────────────────────────────────────────────────────

def _button_release(duo) -> dict:
    """Основа на шаге передачи бота: всё подтверждено — released без реплики."""
    rm.to_step(duo.a, "release")
    built = duo.a.reserve_release_build()
    return built


def test_a_button_move_hands_over_without_a_replica_and_the_guest_takes_the_epoch_after_catching_up(duo):
    """Гость следует за основой: её правки после снимка приходят через резерв;
    `released{epoch, last}` — гость догнал по last, эпоха его (вебхук — один раз)."""
    _ivan(duo)
    duo.open_guest(button=True)
    with duo.as_guest():
        assert duo.gdb.jrn_meta()["role"] == "follower"
    duo.edit("Пётр")
    at_b = duo.a_to_b(duo.a.reserve_jrn_tick())
    duo.b_to_a(at_b)
    assert to_guest(at_b, "jev"), "события основы не ушли гостю при поднятом госте"
    assert duo.b_to_guest(at_b) == [], "гость в следовании подтверждает основе сам"
    built = _button_release(duo)
    assert built is not None and built["blob"] == "" and built["last"] == duo.a.db.jrn_last_i(), built
    checked("released", {"gen": built["gen"], "i": 0, "n": 1, "blob": "", "epoch": built["epoch"], "last": built["last"]})
    m = duo.a.db.jrn_meta()
    assert (m["epoch"], m["holder"], m["role"]) == (built["epoch"], "peer", "follower"), m
    acts = duo.b.reserve_released_jrn_store(built["epoch"], built["last"])
    duo.b_to_guest(acts)
    with duo.as_guest():
        gm = duo.gdb.jrn_meta()
        assert (gm["epoch"], gm["holder"], gm["role"]) == (built["epoch"], "self", "holder"), gm
        assert duo.g.guest_jrn_take_tg() is True and duo.g.guest_jrn_take_tg() is False, "вебхук не один раз"
        duo.g.db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V")
    assert [c for c in duo.gdb.list_clients() if c.name == "Пётр"], "правка основы до передачи не у гостя"
    _guest_round(duo)
    assert _diff(duo) == [], "после переезда кнопкой базы разошлись"


def test_a_guest_behind_asks_the_reserve_and_takes_the_epoch_only_after(duo):
    """Пачка основы до гостя не дошла — по released он не берёт эпоху, просит
    `jneed`, резерв досылает из хранилища, гость догоняет и только тогда держит."""
    _ivan(duo)
    duo.open_guest(button=True)
    duo.edit("Пётр")
    duo.b_to_a(duo.a_to_b(duo.a.reserve_jrn_tick()))     # гостю не передали
    built = _button_release(duo)
    replies = duo.b_to_guest(duo.b.reserve_released_jrn_store(built["epoch"], built["last"]))
    with duo.as_guest():
        assert duo.gdb.jrn_meta()["role"] == "follower", "гость взял эпоху, не догнав основу"
    assert [k for k, _ in replies] == ["jneed"], replies
    resend = duo.guest_to_b(replies)
    assert to_guest(resend, "jev"), "резерв не дослал гостю из хранилища"
    duo.b_to_guest(resend)
    with duo.as_guest():
        duo.g.guest_jrn_tick()
        gm = duo.gdb.jrn_meta()
        assert (gm["holder"], gm["role"]) == ("self", "holder"), gm
    assert [c for c in duo.gdb.list_clients() if c.name == "Пётр"]


def test_the_hand_over_waits_while_events_are_unacked(duo):
    _ivan(duo)
    duo.open_guest(button=True)
    duo.edit("Пётр")
    duo.a.reserve_jrn_tick()                               # ушло, jack нет
    assert _button_release(duo) is None, "бот передан с неподтверждёнными правками"
    assert duo.a.db.jrn_meta()["role"] == "holder"


def test_released_outside_of_taking_is_ignored_by_the_reserve(duo):
    _ivan(duo)
    assert duo.b.reserve_released_jrn_store(3, 10) == []


# ── возврат без слияния и перезапуска ───────────────────────────────────────

def _return(duo) -> list[tuple]:
    """Возврат: основа просит back_req, гость сдаёт эпоху, released через резерв."""
    duo.a.reserve_set_out("returning", kind="button", gen=7)
    duo.a._out_step("back_req", 300)
    duo.a.reserve_epoch_sync()
    assert duo.a.reserve_following(), "на back_req основа ещё следует"
    duo.b.rsv_update("rsv_in", state="releasing")
    _guest_round(duo)
    with duo.as_guest():
        assert duo.g.guest_jrn_drained()
        rel = duo.g.guest_jrn_release()
    acts = duo.b.reserve_guest_released_jrn(rel)
    (kind, body), = [(a[0], a[1]) for a in acts]
    assert kind == "send_released" and body["blob"] == "", acts
    checked("released", {"gen": body["gen"], "i": 0, "n": 1, "blob": "", "epoch": body["epoch"], "last": body["last"]})
    return duo.a.reserve_released_jrn_in(body["epoch"], body["last"])


def test_the_return_takes_the_epoch_without_merge_or_restart_and_the_bases_are_equal(duo, monkeypatch):
    _cid, did, _addr = _ivan(duo)
    _away(duo)
    monkeypatch.setattr(duo.a, "_return_merge", lambda *a, **k: pytest.fail("слияние на пути журнала"))
    with duo.as_guest():
        duo.g.db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V")
        duo.g.db.update_device_fields(did, name="Телефон Ивана")
    acts = _return(duo)
    assert acts == [("take_tg",), ("live",)], f"возврат с журналом идёт не сразу к Telegram: {acts}"
    out = duo.a.rsv_get("rsv_out")
    assert out["step"] == "take_tg" and out["guest_stopped"]
    assert out["merge_report"]["clients"] == 1 and out.get("jrn_report") is None, out
    with duo.as_guest():
        gm = duo.gdb.jrn_meta()
        assert (gm["holder"], gm["role"]) == ("peer", "follower"), f"гость после возврата пишет решения: {gm}"
        tail = duo.gdb._connection().execute("SELECT COUNT(*) FROM jrn_tail").fetchone()[0]
        assert tail == 0, "свои события гостя ушли в хвост — досылать основе нечего"
        with pytest.raises(sqlite3.IntegrityError, match="^jrn:"):
            duo.g.db.create_client("Поздняя", 1, _START, _FUTURE, "INV-DUMMY-L")
    duo.a.reserve_epoch_sync()
    m = duo.a.db.jrn_meta()
    assert (m["holder"], m["role"]) == ("self", "holder"), m
    assert m["epoch"] > gm["epoch"] - 1, m
    assert _diff(duo) == [], "база основы после возврата не равна базе гостя"
    duo.a.db.create_client("После", 1, _START, _FUTURE, "INV-DUMMY-P")   # основа снова пишет решения


def test_a_repeated_back_req_gets_the_same_released(duo):
    _ivan(duo)
    _away(duo)
    _return(duo)
    again = duo.b.reserve_back_req_in()
    (kind, body), = again
    assert kind == "send_released" and body["blob"] == "" and "epoch" in body and "last" in body, again


def test_released_outside_of_a_return_is_ignored(duo):
    _ivan(duo)
    _away(duo)
    assert duo.a.reserve_released_jrn_in(5, 10) == []
    assert duo.a.reserve_following()


# ── правки по ревью: возврат с догоном ──────────────────────────────────────

def _return_lagging(duo, monkeypatch):
    """Возврат, при котором последняя пачка гостя до основы не дошла: released
    по last, а применено меньше."""
    import types
    from awgbot.domain.services.reserve.journal import handover
    monkeypatch.setattr(handover, "time", types.SimpleNamespace(monotonic=duo.clock.monotonic))
    _ivan(duo)
    _away(duo)
    duo.a.reserve_set_out("returning", kind="button", gen=7)
    duo.a._out_step("back_req", 300)
    duo.b.rsv_update("rsv_in", state="releasing")
    with duo.as_guest():
        duo.g.db.create_client("Вера", 1, _START, _FUTURE, "INV-DUMMY-V")
        duo.g.guest_jrn_tick()                             # потерялось по дороге
        e_before = duo.gdb.jrn_meta()["epoch"]
        rel = duo.g.guest_jrn_release()
        gm = duo.gdb.jrn_meta()
        assert (gm["epoch"], gm["holder"], gm["role"]) == (e_before + 1, "peer", "follower"), gm
        assert duo.gdb.jrn_events_after(gm["acked"]), "неподтверждённые события гостя убраны из базы при сдаче эпохи"
    (_k, body), = duo.b.reserve_guest_released_jrn(rel)
    return body, duo.a.reserve_released_jrn_in(body["epoch"], body["last"])


def test_a_return_behind_the_guest_does_not_take_the_epoch_and_asks(duo, monkeypatch):
    body, acts = _return_lagging(duo, monkeypatch)
    applied = duo.a.db.jrn_meta()["applied"]
    assert applied < body["last"]
    assert sends(acts) == [("jneed", {"line": duo.line, "from": applied + 1})], acts
    assert ("take_tg",) not in acts and duo.a.rsv_get("rsv_out")["step"] == "back_req"
    duo.a.reserve_epoch_sync()
    assert duo.a.reserve_following(), "основа взяла эпоху, не догнав правки бота на резерве"


def test_a_return_behind_catches_up_from_the_guest_and_then_takes_the_epoch(duo, monkeypatch):
    """Просьба досылки доходит до гостя (он уже без права записи, но своё досылает),
    основа догоняет — и только тогда эпоха её; базы равны."""
    _body, acts = _return_lagging(duo, monkeypatch)
    to_g = duo.a_to_b(acts)
    assert to_guest(to_g, "jneed"), "просьба досылки не ушла гостю"
    duo.b_to_guest(to_g)
    with duo.as_guest():
        msgs = duo.g.guest_jrn_tick()
    assert [k for k, _ in msgs] == ["jev"], f"гость без права записи не досылает своё: {msgs}"
    at_a = duo.b_to_a(duo.guest_to_b(msgs))
    assert ("take_tg",) in at_a, f"догнала — а эпоха не взята: {at_a}"
    assert [c for c in duo.a.db.list_clients() if c.name == "Вера"]
    duo.a.reserve_epoch_sync()
    assert duo.a.db.jrn_meta()["role"] == "holder"
    assert _diff(duo) == []


def test_a_return_that_never_catches_up_takes_the_epoch_after_the_step_deadline(duo, monkeypatch):
    """Досылка не пришла за срок шага — эпоха всё равно сюда, номера — после последнего
    у бота на резерве (его недостающее потом не примется за своё)."""
    from awgbot.domain.services.reserve.outbound import RELEASED_WAIT_S
    body, _acts = _return_lagging(duo, monkeypatch)
    duo.clock.advance(11)
    out = duo.a.rsv_get("rsv_out")
    assert sends(duo.a.reserve_jrn_back_tick(out), "jneed"), "просьба досылки не повторяется"
    duo.clock.advance(RELEASED_WAIT_S + 1)
    acts = duo.a.reserve_jrn_back_tick(duo.a.rsv_get("rsv_out"))
    assert ("take_tg",) in acts, acts
    assert duo.a.db.jrn_last_i() >= body["last"], "номера основы могут совпасть с номерами бота на резерве"


# ── правки по ревью: гость и вебхук ─────────────────────────────────────────

def test_a_following_guest_does_not_set_the_webhook_and_a_takeover_gives_it_the_epoch(duo):
    """Гость следует (кнопка): вебхук не его; основа умерла, не передав бота
    (takeover от резерва) — эпоха + 1 его, вебхук — один раз."""
    _ivan(duo)
    duo.open_guest(button=True)
    with duo.as_guest():
        assert duo.g.guest_can_take_tg() is False, "следующий гость ставит вебхук"
        e = duo.gdb.jrn_meta()["epoch"]
        duo.g.guest_jrn_in("takeover", {})
        m = duo.gdb.jrn_meta()
        assert (m["epoch"], m["holder"], m["role"]) == (e + 1, "self", "holder"), m
        assert duo.g.guest_can_take_tg() is True
        assert duo.g.guest_jrn_take_tg() is True and duo.g.guest_jrn_take_tg() is False
        duo.g.guest_jrn_in("takeover", {})
        assert duo.gdb.jrn_meta()["epoch"] == e + 1, "повторный takeover поднял эпоху ещё раз"


def test_the_hand_over_is_repeated_to_a_guest_that_reconnects(duo):
    """Передача бота живёт у гостя только в памяти: на его hello резерв повторяет
    released; при tg_take_forced — takeover."""
    _ivan(duo)
    duo.open_guest(button=True)
    assert duo.b.reserve_guest_hello_jrn() == []
    duo.b.reserve_released_jrn_store(7, 42)
    assert to_guest(duo.b.reserve_guest_hello_jrn()) == [("released", {"line": duo.line, "epoch": 7, "last": 42})]
    duo.b.rsv_update("rsv_in", tg_take_forced=True)
    assert to_guest(duo.b.reserve_guest_hello_jrn()) == [("takeover", {})]


# ── дома: зависший вебхук ───────────────────────────────────────────────────

def _ago(minutes: float) -> str:
    import datetime
    from awgbot.util import timeutil
    return timeutil.to_iso(timeutil.now() - datetime.timedelta(minutes=minutes))


def test_at_home_a_stuck_webhook_of_the_reserve_is_taken_after_ten_minutes(mv):
    """Дома Telegram сказал «вебхук не здесь» (основа следует), запись на мне, резерв
    пользователей не несёт — вебхук снимается (take_home), иначе основа без права
    записи бессрочно; раньше 10 минут — нет."""
    svc = mv.svc
    svc.reserve_set_out("home", step="")
    svc.reserve_set_poll("away")
    mv.dns.auth = [rm.OWN_IP]
    rm.st_in(svc, **{"in": "idle"})
    first = svc.reserve_out_tick()
    assert ("take_home",) not in first and svc.rsv_get("rsv_out").get("stuck_since")
    svc.rsv_update("rsv_out", stuck_since=_ago(9))
    mv.clock.advance(169)
    assert ("take_home",) not in svc.reserve_out_tick(), "вебхук снят раньше 10 минут"
    svc.rsv_update("rsv_out", stuck_since=_ago(11))
    mv.clock.advance(169)
    assert ("take_home",) in svc.reserve_out_tick(), "дома зависший вебхук не снят"


def test_at_home_the_webhook_is_not_taken_while_the_reserve_serves(mv):
    svc = mv.svc
    svc.reserve_set_out("home", step="")
    svc.reserve_set_poll("away")
    mv.dns.auth = [rm.OWN_IP]
    rm.st_in(svc, **{"in": "serving"})
    svc.reserve_out_tick()
    svc.rsv_update("rsv_out", stuck_since=_ago(30))
    mv.clock.advance(169)
    assert ("take_home",) not in svc.reserve_out_tick()
