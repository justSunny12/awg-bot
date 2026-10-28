"""Захват решений журналом данных резерва: временные триггеры соединения пишут
событие той же транзакцией, что и запись (util/rsvjrn/triggers.py, классы —
util/rsvjrn/classes.py), операции держателя над счётчиками и дельты
счётчиков — infra/db/jrn.py, infra/db/traffic.py.

Цена ошибки: решение, прошедшее без события, следующий не повторит — основа в
отъезде истечёт продлённую на резерве подписку, бот на резерве не узнает о
заблокированном на основе устройстве; событие, пережившее откат своей
транзакции, — следующий применит решение, которого у держателя нет; закрытый
ключ устройства в событии основы — секрет уезжает на резерв вопреки решению
«не передавать»; подписи и локальные подсети шлюзов в событиях — резерв
получает то, что ему не нужно и в реплике всегда вырезалось; событие на
каждую запись счётчиков опросом — журнал растёт на тысячи строк в сутки, а
пакет и канал работают маячком; дельта трафика без строки `jrn_counts` —
расход, намеренный на этой стороне, другая не увидит, и лимит по сумме не
сработает ни у кого.
"""
from __future__ import annotations

import sqlite3

import pytest

from awgbot.core import config
from awgbot.infra.db import Database
from awgbot.util import rsvjrn, timeutil
from awgbot.util.rsvjrn import apply as japply
from awgbot.util.rsvjrn import classes, triggers
from tests import jrnequiv

pytestmark = pytest.mark.integration

_FUTURE = "2099-01-01T00:00:00+03:00"
_START = "2026-01-01T00:00:00+03:00"


# ── помощники ───────────────────────────────────────────────────────────────

def capture(db, on: bool = True) -> None:
    conn = db._connection()
    conn.execute("UPDATE jrn_meta SET capture = ? WHERE id = 1", (1 if on else 0,))
    conn.commit()


def top(db) -> int:
    return int(db._connection().execute("SELECT COALESCE(MAX(i), 0) FROM jrn_events").fetchone()[0])


def events(db, after: int = 0, table: str | None = None) -> list[japply.Event]:
    out = jrnequiv.events_after(db._connection(), after)
    return [e for e in out if table is None or e.t == table]


def counts(db) -> list[tuple]:
    return [tuple(r) for r in db._connection().execute(
        "SELECT month, device_id, rx, tx, rf_rx, rf_tx, hs FROM jrn_counts ORDER BY s")]


def calendar() -> str:
    return timeutil.now().strftime("%Y-%m")


@pytest.fixture()
def world(services, make_active_client):
    """Держатель с профилем «Иван» и устройством; захват включён после заведения."""
    db = services.db
    ivan = make_active_client(name="Иван", tg_id=7100)
    dev = services.add_device(ivan.id, "Телефон")
    capture(db)
    return type("W", (), {"svc": services, "db": db, "cid": ivan.id, "did": dev.device_id})


@pytest.fixture()
def guest(tmp_path, monkeypatch):
    """База бота основы на резерве: гостевой режим до открытия соединения."""
    monkeypatch.setattr(config, "GUEST", True)
    g = Database(str(tmp_path / "guest.db"))
    g.init_schema()
    capture(g)
    yield g
    g.close()


def _guest_device(g) -> tuple[int, int]:
    cid = g.create_client("Вера", 3, _START, _FUTURE, "INV-DUMMY")
    did = g.create_device(cid, "Телефон", "DUMMY-pub", "DUMMY-psk", "10.8.1.77", private_key="DUMMY-priv")
    return cid, did


# ── решения: I / U / D ──────────────────────────────────────────────────────

def test_a_new_profile_is_an_insert_event_of_each_of_its_rows(world):
    """Новый профиль — события вставки строки профиля, подписки и квоты: у
    следующего без них профиль не появится."""
    before = top(world.db)
    cid = world.db.create_client("Вера", 3, _START, _FUTURE, "INV-DUMMY")
    got = {(e.t, e.op): e for e in events(world.db, before)}
    assert ("clients", "I") in got and got[("clients", "I")].k == {"id": cid}, got
    assert got[("clients", "I")].c["name"] == "Вера" and got[("clients", "I")].o is None
    assert got[("client_subscription", "I")].c["period_end"] == _FUTURE, "срок нового профиля не в событии"
    assert ("client_quota", "I") in got, "квота нового профиля не в событии"


def test_a_changed_decision_is_an_update_event_with_old_and_new_values(world):
    before = top(world.db)
    old = world.db.get_client(world.cid).period_end
    world.db.update_client_fields(world.cid, period_end=_FUTURE)
    got = events(world.db, before)
    assert [(e.t, e.op) for e in got] == [("client_subscription", "U")], got
    ev = got[0]
    assert ev.k == {"client_id": world.cid}
    assert ev.c["period_end"] == _FUTURE and ev.o["period_end"] == old, (ev.c, ev.o)


def test_a_deleted_profile_is_a_delete_event_of_it_and_of_its_cascaded_rows(world):
    """Удаление профиля уносит каскадом устройства — каждое удаление в журнале
    (следующий с выключенными внешними ключами иначе оставил бы сирот)."""
    before = top(world.db)
    world.db.delete_client(world.cid)
    got = {(e.t, e.op): e for e in events(world.db, before)}
    assert got[("clients", "D")].k == {"id": world.cid} and got[("clients", "D")].o["name"] == "Иван", got
    assert got[("devices", "D")].k == {"id": world.did}, "каскадное удаление устройства мимо журнала"
    assert ("client_subscription", "D") in got and ("device_traffic", "D") in got, sorted(got)


def test_a_device_decision_and_its_limit_are_captured(world):
    before = top(world.db)
    world.db.update_device_fields(world.did, name="Планшет", traffic_limit=123456)
    got = {(e.t, e.op): e for e in events(world.db, before)}
    assert got[("devices", "U")].c["name"] == "Планшет" and got[("devices", "U")].o["name"] == "Телефон"
    lim = got[("device_traffic", "U")]
    assert lim.k == {"device_id": world.did} and lim.c == {"traffic_limit": 123456}, \
        f"в событие лимита попали счётчики или ключ не тот: {lim}"


def test_a_mask_change_is_an_event_of_the_profile_and_of_the_device(world):
    before = top(world.db)
    world.db.client_mask_apply(world.cid, or_bits=4)
    world.db.device_mask_apply(world.did, or_bits=2)
    got = {(e.t, e.op): e for e in events(world.db, before)}
    assert (got[("clients", "U")].c["block_reason"], got[("clients", "U")].o["block_reason"]) == (4, 0)
    assert (got[("devices", "U")].c["block_reason"], got[("devices", "U")].o["block_reason"]) == (2, 0)


def test_the_event_rolls_back_together_with_its_decision(world):
    """Решение и событие — одна транзакция: откат унёс оба."""
    before = top(world.db)
    with pytest.raises(RuntimeError):
        with world.db.transaction():
            world.db.update_client_fields(world.cid, period_end=_FUTURE)
            raise RuntimeError("сбой посреди транзакции")
    assert events(world.db, before) == [], "событие пережило откат своего решения"
    assert world.db.get_client(world.cid).period_end != _FUTURE


def test_writing_the_same_value_writes_no_event(world):
    """Сохранение формы без правки (то же имя) — не решение: событий нет."""
    before = top(world.db)
    world.db.update_device_fields(world.did, name="Телефон")
    with world.db._tx() as cur:
        cur.execute("UPDATE clients SET name = name WHERE id = ?", (world.cid,))
    assert events(world.db, before) == [], events(world.db, before)


# ── server_state ────────────────────────────────────────────────────────────

def test_a_decision_key_of_the_server_state_is_captured_and_others_are_not(world):
    """Ключи белого списка (решение о резолвере, почта выхода из паузы) — события;
    стрики, онлайн-счётчик и выбор слота шлюзов — местные."""
    key = classes.STATE_DECISION[0]
    before = top(world.db)
    world.db.set_state(key, "DUMMY-1")
    world.db.set_state(key, "DUMMY-2")
    world.db.set_state("online_count", "3")
    world.db.set_state("routing_active_gateway", "2")
    got = events(world.db, before)
    assert [(e.t, e.op, e.k) for e in got] == [("server_state", "I", {"key": key}), ("server_state", "U", {"key": key})], got
    assert got[1].c == {"value": "DUMMY-2"} and got[1].o == {"value": "DUMMY-1"}


def test_the_spare_slot_choice_of_the_guest_is_not_an_event(guest):
    """Выбор запасного слота «шлюзы резерва» (30) — свойство зоны резерва: у
    основы после возврата остаётся её выбор."""
    before = top(guest)
    guest.set_state("routing_active_gateway", "30")
    guest.set_state("routing_manual_hold", "1")
    assert events(guest, before) == [], events(guest, before)


# ── секреты и история ───────────────────────────────────────────────────────

def test_the_private_key_of_the_base_never_reaches_an_event(world):
    before = top(world.db)
    did = world.db.create_device(world.cid, "Ноутбук", "DUMMY-pub", "DUMMY-psk", "10.8.1.77", private_key="DUMMY-priv")
    with world.db._tx() as cur:
        cur.execute("UPDATE devices SET private_key = 'DUMMY-priv2' WHERE id = ?", (did,))
    got = events(world.db, before, "devices")
    assert got and got[0].op == "I" and got[0].k == {"id": did}, got
    assert all("private_key" not in (e.c or {}) and "private_key" not in (e.o or {}) for e in got), \
        f"закрытый ключ основы в событии: {got}"
    assert not any("DUMMY-priv" in (e.row()[5] or "") + (e.row()[6] or "") for e in events(world.db, before))


def test_the_guests_device_brings_its_private_key(guest):
    """Устройство, заведённое на резерве, основе нужно с ключом — иначе после
    возврата его конфигурацию не собрать."""
    before = top(guest)
    _cid, did = _guest_device(guest)
    ins = [e for e in events(guest, before, "devices") if e.op == "I"]
    assert ins and ins[0].c.get("private_key") == "DUMMY-priv", ins


def test_history_is_captured_by_the_guest_only_as_inserts(world, guest):
    """История едет только от бота основы на резерве — дописками; у основы
    она не событие, чистка по сроку — своя у каждой стороны."""
    before = top(world.db)
    world.db.archive_block(world.cid, 4, "тест")
    assert events(world.db, before) == [], "история основы ушла событием"
    cid, _did = _guest_device(guest)
    g0 = top(guest)
    guest.archive_block(cid, 4, "тест")
    got = events(guest, g0)
    assert [(e.t, e.op) for e in got] == [("client_block_histories", "I")], got
    assert got[0].k == {} and got[0].c["client_id"] == cid and got[0].c["block_reason"] == 4, \
        f"дописка несёт свой номер строки или не несёт содержимого: {got[0]}"
    assert "id" not in got[0].c
    g1 = top(guest)
    guest.purge_histories("2999-01-01T00:00:00+03:00")
    assert events(guest, g1) == [], "чистка истории по сроку у гостя ушла событием"


# ── хост основы ─────────────────────────────────────────────────────────────

def test_a_gateway_slot_travels_without_its_lan_subnets_label_and_mode(world):
    """Слот шлюза — данные хоста основы для гостя, но без локальных подсетей,
    подписи и режима «без VPN» (их резерву не передают)."""
    before = top(world.db)
    slot = world.db.gateway_add(world.did, "awglink", 443, "10.99.99.0/30")
    ins = [e for e in events(world.db, before, "gateways") if e.op == "I"]
    assert ins and ins[0].k == {"id": slot.id} and ins[0].c["link_port"] == 443, ins
    mid = top(world.db)
    world.db.gateway_update(slot.id, label="DUMMY-place", lan_subnets=["192.168.50.0/24"], lan_mode=1)
    assert events(world.db, mid) == [], f"подпись или подсети шлюза — событием: {events(world.db, mid)}"
    world.db.gateway_update(slot.id, link_port=8443)
    world.db.gateway_delete(slot.id)
    got = events(world.db, before, "gateways")
    for e in got:
        for side in (e.c or {}), (e.o or {}):
            assert not {"lan_subnets", "label", "lan_mode"} & set(side), f"обрезанная колонка в событии: {e}"
    assert [e.op for e in got] == ["I", "U", "D"], got
    assert got[1].c["link_port"] == 8443 and got[1].o["link_port"] == 443


def test_the_guest_does_not_send_host_data_back(guest):
    _cid, did = _guest_device(guest)
    before = top(guest)
    guest.gateway_add(did, "awglink", 443, "10.99.99.0/30")
    assert events(guest, before, "gateways") == [], "слоты шлюзов гостя едут основе"


# ── счётчики: не события, а jrn_counts ──────────────────────────────────────

def test_counter_polling_writes_deltas_but_no_events(world):
    """Опрос трафика, РФ-часть и рукопожатие — дельты в jrn_counts той же
    транзакцией; событий не прибавилось ни одного."""
    db, did = world.db, world.did
    before = top(db)
    db.add_traffic_bulk([(did, 1500, 700)])
    db.rf_add_bulk([(did, 30, 40)])
    db.update_device_fields(did, last_handshake=1_700_000_000)
    assert events(db, before) == [], f"запись счётчиков стала событием: {events(db, before)}"
    m = calendar()
    assert counts(db) == [(m, did, 1500, 700, 0, 0, None), (m, did, 0, 0, 30, 40, None),
                          (m, did, 0, 0, 0, 0, 1_700_000_000)], counts(db)


def test_only_the_winner_of_the_first_handshake_writes_a_delta(world):
    """«Увидел впервые» достаётся одному — и строка рукопожатия одна."""
    db, did = world.db, world.did
    assert db.claim_first_handshake(did, "1700000100") is True
    assert db.claim_first_handshake(did, "1700000200") is False
    assert counts(db) == [(calendar(), did, 0, 0, 0, 0, 1_700_000_100)], counts(db)


def test_the_delta_month_is_the_month_of_the_last_reset(world):
    """Метка дельты — месяц последнего сброса, а не календарь: дельта, снятая до
    сброса (он бывает и не 1-го), уйдёт у получателя в архив своего месяца."""
    db, did = world.db, world.did
    db.add_traffic_bulk([(did, 1, 1)])                      # сброса ещё не было — календарь
    db.reset_month_traffic_all("2026-08")
    db.add_traffic_bulk([(did, 2, 2)])
    db.reset_month_traffic_all("2026-09")
    db.add_traffic_bulk([(did, 3, 3)])
    assert [(r[0], r[2]) for r in counts(db)] == [(calendar(), 1), ("2026-08", 2), ("2026-09", 3)], counts(db)


@pytest.mark.jrn_raw("захват выключен нарочно")
def test_capture_off_writes_neither_events_nor_deltas(world):
    """Пара без журнала (capture = 0) — прежний путь: ни событий, ни дельт, а
    сами записи идут как раньше."""
    db, did = world.db, world.did
    capture(db, False)
    before = top(db)
    db.update_client_fields(world.cid, period_end=_FUTURE)
    db.add_traffic_bulk([(did, 10, 10)])
    db.reset_month_traffic_all("2026-11")
    assert events(db, before) == [] and counts(db) == [], (events(db, before), counts(db))
    assert db.get_client(world.cid).period_end == _FUTURE
    assert db.get_state("last_monthly_reset") == "2026-11"


@pytest.mark.jrn_raw("пишет под отметкой применителя нарочно")
def test_the_appliers_mark_silences_capture(world):
    """Внутри транзакции применения (applying = 1) записи — чужие решения: свои
    события и дельты применитель не пишет (иначе эхо ушло бы назад держателю)."""
    db, did = world.db, world.did
    before = top(db)
    with db._tx() as cur:
        cur.execute("UPDATE jrn_meta SET applying = 1 WHERE id = 1")
        db.update_client_fields(world.cid, period_end=_FUTURE)
        db.add_traffic_bulk([(did, 10, 10)])
        db.reset_period_traffic(world.cid)
        cur.execute("UPDATE jrn_meta SET applying = 0 WHERE id = 1")
    assert events(db, before) == [] and counts(db) == [], (events(db, before), counts(db))
    assert db.get_client(world.cid).period_end == _FUTURE, "под отметкой применителя запись не прошла"


# ── операции держателя над счётчиками ───────────────────────────────────────

def _traffic(db, did) -> tuple:
    return tuple(db._connection().execute(
        "SELECT traffic_rx_month, traffic_tx_month, traffic_rx_period, traffic_tx_period, rf_rx_month, rf_tx_month "
        "FROM device_traffic WHERE device_id = ?", (did,)).fetchone())


def test_the_month_reset_is_one_event_and_marks_the_month(world):
    db, did = world.db, world.did
    db.add_traffic_bulk([(did, 100, 50)])
    before = top(db)
    db.reset_month_traffic_all("2026-11")
    got = events(db, before)
    assert [(e.t, e.op, e.k) for e in got] == [("device_traffic", "month", {"month": "2026-11"})], got
    assert db.get_state("last_monthly_reset") == "2026-11", "отметка месяца сброса не той транзакцией"
    assert _traffic(db, did)[:2] == (0, 0) and _traffic(db, did)[2:4] == (100, 50)


def test_the_month_reset_without_a_month_marks_the_calendar(world):
    world.db.reset_month_traffic_all()
    assert world.db.get_state("last_monthly_reset") == calendar()
    assert events(world.db)[-1].k == {"month": calendar()}


def test_the_period_reset_and_the_pair_merge_are_events(world):
    db, did = world.db, world.did
    other = world.svc.add_device(world.cid, "Ноутбук").device_id
    db.add_traffic_bulk([(did, 100, 50), (other, 7, 3)])
    before = top(db)
    db.merge_traffic(did, other)
    db.reset_period_traffic(world.cid)
    got = events(db, before)
    assert [(e.op, e.k) for e in got] == [("tmerge", {"src": did, "dst": other}), ("period", {"client_id": world.cid})], got
    assert _traffic(db, other)[:2] == (107, 53) and _traffic(db, other)[2:4] == (0, 0)


@pytest.mark.parametrize("op", ["month", "period", "tmerge"])
def test_the_holder_operations_are_refused_to_the_follower(world, op):
    """Сброс месяца, сброс периода и сложение пары — решения держателя: у
    следующего отказ, счётчики и отметка месяца не тронуты, событий нет."""
    db, did = world.db, world.did
    other = world.svc.add_device(world.cid, "Ноутбук").device_id
    db.add_traffic_bulk([(did, 100, 50), (other, 7, 3)])
    db.jrn_pass(holder="peer", role="follower", why="тест: следование")
    before, mark = top(db), db.get_state("last_monthly_reset")
    snap = (_traffic(db, did), _traffic(db, other))
    call = {"month": lambda: db.reset_month_traffic_all("2026-11"),
            "period": lambda: db.reset_period_traffic(world.cid),
            "tmerge": lambda: db.merge_traffic(did, other)}[op]
    with pytest.raises(sqlite3.IntegrityError) as ei:
        call()
    assert rsvjrn.is_refusal(ei.value), f"отказ без префикса jrn: {ei.value}"
    assert (_traffic(db, did), _traffic(db, other)) == snap, "следующий сбросил или сложил счётчики"
    assert db.get_state("last_monthly_reset") == mark and events(db, before) == []


# ── реестр классов ──────────────────────────────────────────────────────────

def test_the_registry_names_no_table_or_column_that_is_not_in_the_schema(db):
    """Лишних в реестре нет: таблица или колонка, которой в схеме нет, — след
    переименования, после которого настоящая колонка ушла в «по умолчанию»."""
    schema = {t: [c for c, _ in spec] for t, spec in triggers.schema_of(db._connection()).items()}
    stale = sorted(set(classes.TABLES) - set(schema))
    assert stale == [], f"в реестре таблицы, которых нет в схеме: {stale}"
    for table, spec in classes.TABLES.items():
        if isinstance(spec, dict):
            named = sorted(set(spec) - {"*"} - set(schema[table]))
            assert named == [], f"{table}: в реестре колонки, которых нет: {named}"
    for table in classes.HOST_TRIMMED:
        assert set(classes.HOST_TRIMMED[table]) <= set(schema[table]), classes.HOST_TRIMMED


# Колонки смешанных таблиц, которые берут класс «по умолчанию» (`*`). Новая колонка
# здесь молча станет решением (clients, devices) или счётчиком (device_traffic) —
# сторож требует посмотреть на неё глазами и дописать сюда или в реестр.
_DEFAULTED = {
    "clients": {"id", "tg_id", "name", "device_limit", "activation_status", "invite_code", "is_service",
                "created_at", "kind", "tg_name", "tg_name_at", "tg_username", "routing_allowed"},
    "devices": {"id", "client_id", "name", "public_key", "preshared_key", "address", "routing_on", "iface",
                "is_gateway", "twin_of", "created_at", "holder_client_id"},
    "device_traffic": {"device_id", "traffic_rx_month", "traffic_tx_month", "traffic_rx_period",
                       "traffic_tx_period", "rf_rx_month", "rf_tx_month", "last_handshake"},
}


def test_a_new_column_of_a_mixed_table_does_not_silently_take_the_default_class(db):
    schema = {t: [c for c, _ in spec] for t, spec in triggers.schema_of(db._connection()).items()}
    mixed = {t for t, spec in classes.TABLES.items() if isinstance(spec, dict) and "*" in spec}
    assert mixed == set(_DEFAULTED), f"смешанные таблицы реестра изменились: {sorted(mixed)}"
    for table in mixed:
        defaulted = {c for c in schema[table] if c not in classes.TABLES[table]}
        assert defaulted == _DEFAULTED[table], \
            f"{table}: новая колонка без явного класса {sorted(defaulted - _DEFAULTED[table])} " \
            f"(или пропала {sorted(_DEFAULTED[table] - defaulted)})"


# ── временные триггеры ──────────────────────────────────────────────────────

@pytest.mark.jrn_raw("пишет сырым соединением нарочно")
def test_the_capture_triggers_live_only_in_the_connection(world):
    """Триггеры захвата — временные: в файле базы их нет, сырое соединение
    (реплика, бэкап) их не видит и событий не пишет."""
    conn = world.db._connection()
    temp = {r[0] for r in conn.execute("SELECT name FROM sqlite_temp_master WHERE type = 'trigger'")}
    assert {"jrn_cap_clients_u", "jrn_cap_devices_i", "jrn_cap_gateways_d", "jrn_cap_state_u", "jrn_deny_clients_u"} <= temp, sorted(temp)
    before = top(world.db)
    raw = sqlite3.connect(world.db.path)
    try:
        assert raw.execute("SELECT count(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0] == 0
        assert raw.execute("SELECT count(*) FROM sqlite_temp_master WHERE type = 'trigger'").fetchone()[0] == 0
        raw.execute("UPDATE clients SET name = 'Сырое' WHERE id = ?", (world.cid,))
        raw.commit()
    finally:
        raw.close()
    assert events(world.db, before) == [], "сырое соединение записало событие — триггер ушёл в файл базы"
