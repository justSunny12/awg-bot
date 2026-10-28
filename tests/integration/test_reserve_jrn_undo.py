"""Откат своего хвоста у бывшего держателя (util/rsvjrn/apply.undo,
infra/db/jrn.jrn_tail_finalize, journal/tail.reserve_tail_finalize) и пустой
секрет в событии у следующего (apply: исправление этапа 3).

Бывший держатель, уходя в следование от чужого держателя, откладывает свои
неподтверждённые события в хвост (`hold`) — без отката. Когда точка
расхождения F известна: события ≤ F у держателя есть — прочь из хвоста, в
базе остаются; > F — откат в базе по прежним значениям обратным порядком,
хвост — к отправке (`wait`); применённое — не ниже F. Закрытые ключи
устройств, которые откат удалит, ждут их возвращения событиями держателя.

Цена ошибки: откат не тех событий — правки, которые у держателя уже есть, не
вернутся никогда (или вернутся дважды); откат прямым порядком — у строки,
правленной дважды, остаётся промежуточное значение, и события держателя
ложатся на базу, которой у него не было; F вне своих номеров (чужое слово) —
откат правок, подтверждённых резервом; закрытый ключ устройства, вернувшегося
событием держателя без ключа, — конфигурацию устройства больше не выдать;
пустой ключ в событии бота на резерве, применённый как есть, — стирает ключ
основы у каждого устройства, которое там правили.
"""
from __future__ import annotations

import json

import pytest

from awgbot.util.rsvjrn import apply as japply

from tests.integration import _reserve_common as rc

pytestmark = pytest.mark.integration
rsv = rc.rsv

_START = "2026-01-01T00:00:00+03:00"
_FUTURE = "2099-01-01T00:00:00+03:00"


def _capture(db) -> None:
    conn = db._connection()
    conn.execute("UPDATE jrn_meta SET capture = 1 WHERE id = 1")
    conn.commit()


def _q(db, sql, args=()):
    return [tuple(r) for r in db._connection().execute(sql, args).fetchall()]


@pytest.fixture()
def held(db):
    """Держатель с подтверждённым началом (Аня, Борис с устройствами) и хвостом
    после подтверждённого: правка имени Ани дважды, новый профиль Вера с
    устройством (с закрытым ключом), удаление устройства Бориса; уход в следование
    — хвост отложен."""
    _capture(db)
    a = db.create_client("Аня", 3, _START, _FUTURE, "INV-DUMMY-A")
    b = db.create_client("Борис", 3, _START, _FUTURE, "INV-DUMMY-B")
    da = db.create_device(a, "Телефон", "DUMMY-pub-a", "DUMMY-psk", "10.8.1.2", private_key="DUMMY-priv-a")
    dbb = db.create_device(b, "Ноутбук", "DUMMY-pub-b", "DUMMY-psk", "10.8.1.3", private_key="DUMMY-priv-b")
    acked = db.jrn_last_i()
    db.jrn_acked_set(acked)
    db.update_client_fields(a, name="Аня К.")
    mid = db.jrn_last_i()
    db.update_client_fields(a, name="Анна Каренина")
    v = db.create_client("Вера", 3, _START, _FUTURE, "INV-DUMMY-V")
    dv = db.create_device(v, "Планшет", "DUMMY-pub-v", "DUMMY-psk", "10.8.1.4", private_key="DUMMY-priv-v")
    db.delete_device(dbb)
    top = db.jrn_last_i()
    db.jrn_pass(holder="peer", role="follower", why="тест: эпоха ушла", fork_i=acked)
    return {"a": a, "b": b, "da": da, "db": dbb, "v": v, "dv": dv, "acked": acked, "mid": mid, "top": top}


def test_going_follower_holds_the_tail_without_undoing_it(db, held):
    """Точки расхождения ещё нет — база не трогается (иначе откатились бы правки,
    которые у держателя есть, и не вернулись бы)."""
    assert db.jrn_tail_held()
    assert _q(db, "SELECT name FROM clients WHERE id = ?", (held["a"],)) == [("Анна Каренина",)]
    assert _q(db, "SELECT COUNT(*) FROM clients WHERE id = ?", (held["v"],)) == [(1,)]
    assert db.jrn_tail_rows() == [], "отложенный хвост уже к отправке"
    assert [e.i for e in db.jrn_tail_rows(("hold",))] == list(range(held["acked"] + 1, held["top"] + 1))


def test_the_fork_at_acked_undoes_the_whole_tail_in_reverse_order(db, held):
    n = db.jrn_tail_finalize(held["acked"])
    assert n == held["top"] - held["acked"]
    assert _q(db, "SELECT name FROM clients WHERE id = ?", (held["a"],)) == [("Аня",)], \
        "дважды правленное имя откатилось не к точке расхождения (порядок отката)"
    assert _q(db, "SELECT COUNT(*) FROM clients WHERE id = ?", (held["v"],)) == [(0,)], "вставка хвоста осталась"
    assert _q(db, "SELECT COUNT(*) FROM devices WHERE id = ?", (held["dv"],)) == [(0,)]
    assert _q(db, "SELECT name, address FROM devices WHERE id = ?", (held["db"],)) == [("Ноутбук", "10.8.1.3")], \
        "удалённое хвостом устройство не вернулось"
    assert _q(db, "SELECT COUNT(*) FROM device_traffic WHERE device_id = ?", (held["db"],)) == [(1,)]
    assert [e.i for e in db.jrn_tail_rows(("wait",))] == list(range(held["acked"] + 1, held["top"] + 1))
    assert not db.jrn_tail_held()
    assert db.jrn_meta()["applied"] == held["acked"]
    assert _q(db, "SELECT applying FROM jrn_meta WHERE id = 1") == [(0,)], "отметка применителя осталась"


def test_events_up_to_the_fork_stay_in_the_base_and_leave_the_tail(db, held):
    """Держатель начал после первой правки хвоста (она у него есть): она остаётся, всё
    после — откатывается; applied = F."""
    db.jrn_tail_finalize(held["mid"])
    assert _q(db, "SELECT name FROM clients WHERE id = ?", (held["a"],)) == [("Аня К.",)], \
        "правка по F откачена (у держателя она есть — не вернётся) или правка после F осталась"
    assert [e.i for e in db.jrn_tail_rows()][0] == held["mid"] + 1
    assert db.jrn_meta()["applied"] == held["mid"]


def test_a_second_finalize_changes_nothing(db, held):
    db.jrn_tail_finalize(held["acked"])
    before = (_q(db, "SELECT id, name FROM clients ORDER BY id"), [e.i for e in db.jrn_tail_rows()])
    assert db.jrn_tail_finalize(held["acked"]) == 0
    assert (_q(db, "SELECT id, name FROM clients ORDER BY id"), [e.i for e in db.jrn_tail_rows()]) == before


def test_applied_never_goes_back(db, held):
    db._connection().execute("UPDATE jrn_meta SET applied = ? WHERE id = 1", (held["top"] + 5,))
    db._connection().commit()
    db.jrn_tail_finalize(held["acked"])
    assert db.jrn_meta()["applied"] == held["top"] + 5, "применённое ушло назад — события держателя применятся дважды"


def test_the_service_skips_a_fork_outside_of_my_numbers(services, rsv, held):
    """F ниже подтверждённого или дальше последнего номера хвоста — не моя точка: ничего
    не откатывать (сломанный резерв или чужое слово откатили бы подтверждённое)."""
    rc.pair_row(services)
    db = services.db
    for bad in (held["acked"] - 1, held["top"] + 1):
        services.reserve_tail_finalize(bad)
        assert db.jrn_tail_held(), f"F={bad} принята"
        assert _q(db, "SELECT name FROM clients WHERE id = ?", (held["a"],)) == [("Анна Каренина",)]
    services.reserve_tail_finalize(held["acked"])
    t = services.rsv_get("rsv_tail")
    assert not db.jrn_tail_held() and (t.get("e"), t.get("F")) == (db.jrn_tail_rows()[0].e, held["acked"]), t


def test_undo_peers_follow_the_base(services, rsv, held, fake_awg):
    """Пиры — за откатом: устройство хвоста — с интерфейса, удалённое хвостом — обратно."""
    rc.pair_row(services)
    fake_awg.peers["DUMMY-pub-v"] = ("DUMMY-psk", "10.8.1.4")
    services.reserve_tail_finalize(held["acked"])
    assert "DUMMY-pub-v" not in fake_awg.peers, "пир откаченного устройства остался"
    assert fake_awg.peers.get("DUMMY-pub-b") == ("DUMMY-psk", "10.8.1.3"), "пир вернувшегося устройства не поставлен"


def test_the_private_key_of_an_undone_insert_waits_and_comes_back_with_the_holders_event(db, held):
    """Откат удаляет устройство Веры с её ключом; держатель возвращает его событием без
    ключа (у бота на резерве ключей основы нет) — ключ ставится на место."""
    db.jrn_tail_finalize(held["acked"])
    stash = json.loads(_q(db, "SELECT value FROM server_state WHERE key = 'rsv_tail_keys'")[0][0])
    assert stash == {"DUMMY-pub-v": "DUMMY-priv-v"}, stash
    i = db.jrn_meta()["applied"]
    e = db.jrn_meta()["epoch"]
    c = {"id": held["v"], "tg_id": None, "name": "Вера", "device_limit": 3, "block_reason": 0,
         "activation_status": "pending", "invite_code": "INV-DUMMY-V", "is_service": 0, "created_at": _START,
         "kind": "owner", "tg_name": "", "tg_name_at": None, "tg_username": "", "routing_allowed": 0}
    dev = {"id": held["dv"], "client_id": held["v"], "name": "Планшет", "public_key": "DUMMY-pub-v",
           "preshared_key": "DUMMY-psk", "address": "10.8.1.4", "block_reason": 0, "routing_on": 0, "iface": "",
           "is_gateway": 0, "twin_of": None, "created_at": _START, "holder_client_id": None, "private_key": None}
    res = db.jrn_apply([japply.Event(i=i + 1, e=e, t="clients", op="I", k={"id": held["v"]}, c=c),
                        japply.Event(i=i + 2, e=e, t="devices", op="I", k={"id": held["dv"]}, c=dev)])
    assert res.lost == [], res.lost
    assert _q(db, "SELECT private_key FROM devices WHERE id = ?", (held["dv"],)) == [(None,)], "условие теста"
    assert db.jrn_keys_restore() == 1
    assert _q(db, "SELECT private_key FROM devices WHERE id = ?", (held["dv"],)) == [("DUMMY-priv-v",)], \
        "ключ вернувшегося устройства не восстановлен"
    assert _q(db, "SELECT value FROM server_state WHERE key = 'rsv_tail_keys'") == [("",)], "ключ остался в запасе"
    assert db.jrn_keys_restore() == 0


def test_an_undone_removal_brings_the_device_back_and_only_inserts_stash_keys(db, held):
    """Удаление устройства хвостом откачено — устройство вернулось (адрес и имя на месте),
    события основы закрытый ключ не несут (секреты основы не захватываются) — ключа у
    вернувшейся строки нет, и в запас он не кладётся: перенесёт держатель удаление —
    устройство уйдёт и здесь; спорное удаление Т-Ж4 просит выдать конфигурацию заново."""
    db.jrn_tail_finalize(held["acked"])
    assert _q(db, "SELECT name, address FROM devices WHERE id = ?", (held["db"],)) == [("Ноутбук", "10.8.1.3")]
    stash = json.loads(_q(db, "SELECT value FROM server_state WHERE key = 'rsv_tail_keys'")[0][0])
    assert "DUMMY-pub-b" not in stash, f"ключ удалённого устройства отложен как ключ вставки: {stash}"


def test_unused_stashed_keys_are_cleared_after_the_return(services, rsv, held):
    """Отложенные ключи устройств, которые так и не вернулись, после возврата — прочь
    (закрытый ключ не лежит в служебной строке бессрочно)."""
    rc.pair_row(services)
    services.db.jrn_tail_finalize(held["acked"])
    assert services.db.get_state("rsv_tail_keys"), "условие: ключ отложен"
    services.reserve_tail_keys_clear()
    assert not services.db.get_state("rsv_tail_keys"), "отложенный закрытый ключ остался после возврата"


def test_host_rows_are_not_undone(db):
    """Слоты шлюзов держатель не вернёт — откат их не трогает."""
    _capture(db)
    a = db.create_client("Аня", 3, _START, _FUTURE, "INV-DUMMY-A")
    da = db.create_device(a, "Телефон", "DUMMY-pub-a", "DUMMY-psk", "10.8.1.2")
    acked = db.jrn_last_i()
    db.jrn_acked_set(acked)
    db.gateway_add(da, "awglink", 443, "10.99.99.0/30")
    db.jrn_pass(holder="peer", role="follower", why="тест", fork_i=acked)
    db.jrn_tail_finalize(acked)
    assert _q(db, "SELECT COUNT(*) FROM gateways") == [(1,)], "слот шлюза откачен"


# ── пустой секрет у следующего ──────────────────────────────────────────────

@pytest.fixture()
def follower(db):
    _capture(db)
    a = db.create_client("Аня", 3, _START, _FUTURE, "INV-DUMMY-A")
    da = db.create_device(a, "Телефон", "DUMMY-pub-a", "DUMMY-psk", "10.8.1.2", private_key="DUMMY-priv-a")
    db.jrn_pass(holder="peer", role="follower", why="тест", fork_i=db.jrn_last_i())
    return {"a": a, "da": da}


def test_an_empty_secret_in_a_holder_event_does_not_erase_the_key(db, follower):
    """Бот на резерве правит устройство — его событие несёт пустой закрытый ключ: у основы
    ключ остаётся, правка ложится."""
    m = db.jrn_meta()
    row = dict(zip(("id", "client_id", "name", "public_key", "preshared_key", "address"),
                   _q(db, "SELECT id, client_id, name, public_key, preshared_key, address FROM devices WHERE id = ?",
                      (follower["da"],))[0]))
    res = db.jrn_apply([japply.Event(i=m["applied"] + 1, e=m["epoch"], t="devices", op="U", k={"id": follower["da"]},
                                     c={**row, "name": "Телефон Ани", "private_key": None},
                                     o={**row, "private_key": None})])
    assert res.lost == [], res.lost
    assert _q(db, "SELECT name, private_key FROM devices WHERE id = ?", (follower["da"],)) == \
        [("Телефон Ани", "DUMMY-priv-a")], "пустой ключ в событии стёр закрытый ключ основы"


def test_an_insert_over_an_existing_row_with_an_empty_secret_keeps_the_key(db, follower):
    m = db.jrn_meta()
    res = db.jrn_apply([japply.Event(i=m["applied"] + 1, e=m["epoch"], t="devices", op="I", k={"id": follower["da"]},
                                     c={"id": follower["da"], "client_id": follower["a"], "name": "Телефон",
                                        "public_key": "DUMMY-pub-a", "preshared_key": "DUMMY-psk",
                                        "address": "10.8.1.2", "private_key": None})])
    assert res.lost == [], res.lost
    assert _q(db, "SELECT private_key FROM devices WHERE id = ?", (follower["da"],)) == [("DUMMY-priv-a",)]


def test_a_real_secret_in_a_holder_event_is_applied(db, follower):
    """Не пустой ключ — решение держателя (смена ключа на резерве): применяется."""
    m = db.jrn_meta()
    db.jrn_apply([japply.Event(i=m["applied"] + 1, e=m["epoch"], t="devices", op="U", k={"id": follower["da"]},
                               c={"private_key": "DUMMY-priv-new"}, o={"private_key": None})])
    assert _q(db, "SELECT private_key FROM devices WHERE id = ?", (follower["da"],)) == [("DUMMY-priv-new",)]
