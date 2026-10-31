"""Переезд профилей на второй интерфейс: рычаг — одно действие за раз, один
двойник на исходное устройство.

Что стережём: включение, завершение и отмена идут из пула потоков
обработчиков; двойное нажатие запускало второе включение параллельно
первому, и каждое исходное устройство получало по два двойника (UNIQUE по
twin_of не было). Цена ошибки: два пира на человека, слияние истории не в ту
строку, финал закрывает одну пару и оставляет вторую висеть. Схема: база
прежних выпусков с дублями обязана открыться (уникальный индекс отложен), а
старт бота — расчистить их и поставить индекс.
"""
import sqlite3
import threading

import pytest

from awgbot.domain.migration import ServiceErrorMigration
from awgbot.domain.services import ServiceError
from awgbot.domain.services.reserve.away import AWAY_REFUSED

from tests.integration._migration_common import _seen, _twin

pytestmark = pytest.mark.integration


# ── замок рычага ─────────────────────────────────────────────────────────────

def test_second_start_during_the_first_is_refused_and_births_nothing(services, mig, make_active_client,
                                                                     monkeypatch):
    """Два потока жмут «начать» — второму отказ словами о том, что идёт, и ровно
    один двойник на устройство."""
    c = make_active_client(name="c", tg_id=7301)
    dc = services.add_device(c.id, "Тел")
    inside, go = threading.Event(), threading.Event()

    def slow_dns(prefix):
        inside.set()
        assert go.wait(5)
        return ""
    monkeypatch.setattr(services, "private_dns_on_migration_start", slow_dns)
    out: dict = {}
    t = threading.Thread(target=lambda: out.__setitem__("res", services.migration_start()))
    t.start()
    assert inside.wait(5)
    with pytest.raises(ServiceErrorMigration, match="уже включается"):
        services.migration_start()
    go.set()
    t.join(5)
    assert out["res"].born == 1
    assert sum(1 for d in services.db.list_all_devices() if d.twin_of == dc.device_id) == 1, "двойник задвоился"


def test_finish_and_cancel_share_the_lever_with_start(services, mig, make_active_client, monkeypatch):
    """Завершение и отмена поверх идущего включения — тот же отказ: иначе отмена
    гасила бы рычаг посреди рождения двойников."""
    c = make_active_client(name="c", tg_id=7302)
    services.add_device(c.id, "Тел")
    inside, go = threading.Event(), threading.Event()

    def slow_dns(prefix):
        inside.set()
        assert go.wait(5)
        return ""
    monkeypatch.setattr(services, "private_dns_on_migration_start", slow_dns)
    t = threading.Thread(target=services.migration_start)
    t.start()
    assert inside.wait(5)
    try:
        with pytest.raises(ServiceErrorMigration, match="уже включается"):
            services.migration_finish()
        with pytest.raises(ServiceErrorMigration, match="уже включается"):
            services.migration_cancel()
    finally:
        go.set()
        t.join(5)
    assert services.migration_running()
    # замок отпущен — следующее действие проходит
    assert services.migration_cancel() == 0


def test_birth_twin_rechecks_the_pair_under_the_mutation_lock(services, mig, make_active_client):
    """Список «без двойника» читается до замка мутаций — рождение перепроверяет
    пару под ним и отдаёт существующего, а не второго."""
    c = make_active_client(name="c", tg_id=7303)
    dc = services.add_device(c.id, "Тел")
    dev = services.db.get_device(dc.device_id)
    first = services._birth_twin(dev)
    assert services._birth_twin(dev) == first
    assert sum(1 for d in services.db.list_all_devices() if d.twin_of == dc.device_id) == 1


def test_schema_refuses_a_second_twin(services, mig, make_active_client):
    """Последний рубеж — сама база: второй двойник на то же исходное не вставить."""
    c = make_active_client(name="c", tg_id=7304)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    with pytest.raises(sqlite3.IntegrityError):
        services.db.create_device(c.id, "Тел", "X" * 43 + "=", "psk", "10.9.1.77",
                                  private_key="p", iface="awg1", twin_of=dc.device_id)


# ── отказ при пользователях на резерве ──────────────────────────────────────

@pytest.mark.parametrize("action", ["migration_start", "migration_finish", "migration_cancel"])
def test_lever_actions_refuse_while_users_are_away(services, mig, make_active_client, monkeypatch, action):
    """Пока пользователи на резерве, переезд не начать, не завершить и не отменить:
    двойники, снятые пиры и слитая история разошлись бы с базой гостя."""
    c = make_active_client(name="c", tg_id=7305)
    dc = services.add_device(c.id, "Тел")
    if action != "migration_start":
        services.migration_start()
        _seen(services, _twin(services, dc.device_id).id, ago_days=0)
    monkeypatch.setattr(services, "reserve_paired", lambda: True)
    monkeypatch.setattr(services, "reserve_away", lambda: True)
    monkeypatch.setattr(services, "reserve_peer_name", lambda: "Резерв")
    with pytest.raises(ServiceError, match=AWAY_REFUSED.format(name="Резерв")[:30]):
        getattr(services, action)()
    if action != "migration_start":
        assert services.migration_running(), "рычаг тронут при отказе"
        assert services.db.get_device(dc.device_id) is not None


# ── откат рождения ───────────────────────────────────────────────────────────

def test_routing_flag_failure_rolls_the_twin_row_back(services, mig, make_active_client, monkeypatch):
    """Отказ записи флага маршрутизации — строки двойника не остаётся: иначе
    повторное включение считало бы пару готовой, а пира на сервере нет."""
    c = make_active_client(name="c", tg_id=7306)
    services.add_device(c.id, "Тел")
    orig = services.db.update_device_fields

    def flaky(device_id, **fields):
        if "routing_on" in fields:
            raise sqlite3.OperationalError("database is locked")
        return orig(device_id, **fields)
    monkeypatch.setattr(services.db, "update_device_fields", flaky)
    res = services.migration_start()
    assert res.born == 0 and res.failed
    assert services.db.twins_by_origin() == {}, "строка двойника без пира осталась"
    assert mig.peers_by_iface.get("awg1", {}) == {}


# ── база прежних выпусков с дублями ──────────────────────────────────────────

def _plant_duplicate_twins(db, origin_id: int, client_id: int, hs=(None, None)) -> list[int]:
    """Копия базы со стенда прежнего выпуска: неуникальный индекс, по два
    двойника на исходное. Пишем сырым соединением — так выглядела бы база,
    открытая новым кодом."""
    con = sqlite3.connect(db.path)
    try:
        con.execute("DROP INDEX IF EXISTS idx_devices_twin_uniq")
        con.execute("CREATE INDEX IF NOT EXISTS idx_devices_twin ON devices(twin_of) WHERE twin_of IS NOT NULL")
        ids = []
        for i, h in enumerate(hs):
            cur = con.execute(
                "INSERT INTO devices (client_id, name, private_key, public_key, preshared_key, address, "
                "iface, twin_of, created_at) VALUES (?, 'Тел', 'p', ?, 'psk', ?, 'awg1', ?, '2026-01-01')",
                (client_id, f"{'D' * 42}{i}=", f"10.9.1.{10 + i}", origin_id))
            con.execute("INSERT INTO device_traffic (device_id, last_handshake) VALUES (?, ?)", (cur.lastrowid, h))
            ids.append(cur.lastrowid)
        con.commit()
        return ids
    finally:
        con.close()


@pytest.mark.jrn_raw("копия базы прежнего выпуска — сырое соединение")
def test_old_db_with_duplicate_twins_opens_and_startup_cleans_it(services, mig, make_active_client):
    """База с дублями обязана открыться (индекс отложен, не падение старта), а
    старт бота — снять лишнего двойника без рукопожатия с сервера и из базы и
    поставить индекс."""
    from awgbot.infra.db import Database
    c = make_active_client(name="c", tg_id=7307)
    dc = services.add_device(c.id, "Тел")
    twins = _plant_duplicate_twins(services.db, dc.device_id, c.id, hs=(None, 1_700_000_000))
    for t in twins:
        mig.peers_by_iface.setdefault("awg1", {})[services.db.get_device(t).public_key] = "x"

    other = Database(services.db.path)
    try:
        other.init_schema()                                     # не падает
        assert other.ensure_twin_unique() is False
        assert other.twin_duplicates() == {dc.device_id: twins}
    finally:
        other.close()

    assert services.migration_dedup_twins() == 1
    keep, extra = twins[1], twins[0]                            # с рукопожатием остаётся
    assert services.db.get_device(extra) is None
    assert services.db.get_device(keep).twin_of == dc.device_id
    assert services.db.twins_by_origin() == {dc.device_id: keep}
    assert "D" * 42 + "0=" not in mig.peers_by_iface["awg1"], "пир лишнего двойника остался на сервере"
    assert services.db.ensure_twin_unique() is True
    assert services.migration_dedup_twins() == 0                 # идемпотентно
    with pytest.raises(sqlite3.IntegrityError):
        services.db.create_device(c.id, "Тел", "Y" * 43 + "=", "psk", "10.9.1.99",
                                  private_key="p", iface="awg1", twin_of=dc.device_id)


@pytest.mark.jrn_raw("копия базы прежнего выпуска — сырое соединение")
def test_extra_twin_with_a_handshake_is_detached_not_dropped(services, mig, make_active_client, caplog):
    """На обоих двойниках кто-то сидел — никого не роняем: лишний отцепляется от
    пары и остаётся обычным устройством, о нём — строка в журнале."""
    c = make_active_client(name="c", tg_id=7308)
    dc = services.add_device(c.id, "Тел")
    twins = _plant_duplicate_twins(services.db, dc.device_id, c.id, hs=(1_700_000_000, 1_700_000_500))
    with caplog.at_level("WARNING"):
        assert services.migration_dedup_twins() == 1
    kept, detached = twins[1], twins[0]
    assert services.db.get_device(detached) is not None and services.db.get_device(detached).twin_of is None
    assert services.db.twins_by_origin() == {dc.device_id: kept}
    assert mig.removed == [], "пир с рукопожатием снят"
    assert any("отцеплен" in r.message for r in caplog.records)


def test_fresh_db_has_the_unique_index_and_not_the_old_one(db):
    names = {r[0] for r in db._connection().execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'devices'")}
    assert "idx_devices_twin_uniq" in names
    assert "idx_devices_twin" not in names
