"""Переезд профилей на второй интерфейс: прогресс и оба выхода — отмена и
завершение.

Инфраструктура подменена — проверяем, ЧТО проецируется наружу и что
остаётся в базе, а не как выглядит вывод awg.
"""
import pytest

from tests.integration._migration_common import _seen, _twin

pytestmark = pytest.mark.integration


# ── прогресс ─────────────────────────────────────────────────────────────────

def test_client_counts_as_moved_only_when_all_live_devices_did(services, mig,
                                                               make_active_client):
    """«Хотя бы одно» дало бы число, которое врёт: человек с двумя телефонами
    считался бы переехавшим, пока второй лежит на старом интерфейсе."""
    c = make_active_client(name="c", tg_id=7008)
    a = services.add_device(c.id, "A")
    b = services.add_device(c.id, "B")
    _seen(services, a.device_id, ago_days=1)
    _seen(services, b.device_id, ago_days=1)
    services.migration_start()

    _seen(services, _twin(services, a.device_id).id, ago_days=0)
    p = services.migration_progress()
    assert (p.devices_done, p.devices_total) == (1, 2)
    assert (p.clients_done, p.clients_total) == (0, 1)

    _seen(services, _twin(services, b.device_id).id, ago_days=0)
    p = services.migration_progress()
    assert (p.clients_done, p.devices_done) == (1, 2) and p.complete


def test_revived_dead_peer_does_not_move_the_denominator(services, mig,
                                                         make_active_client):
    """Оживший мёртвый пир переезжает, но в x/y не попадает — только в общее
    число. Это следствие заморозки, а не ошибка: иначе завершение недостижимо."""
    c = make_active_client(name="c", tg_id=7009)
    live = services.add_device(c.id, "Живое")
    dead = services.add_device(c.id, "Мёртвое")
    _seen(services, live.device_id, ago_days=1)
    services.migration_start()

    _seen(services, _twin(services, live.device_id).id, ago_days=0)
    _seen(services, _twin(services, dead.device_id).id, ago_days=0)
    p = services.migration_progress()
    assert (p.devices_done, p.devices_total) == (1, 1) and p.complete

    done, live_n, total = services.migration_client_progress(c.id)
    assert (done, live_n) == (1, 1)
    assert total == 2, "всего считается парами, а не строками"


def test_pending_list_names_who_is_left(services, mig, make_active_client):
    """Реальность выглядит как «11 из 12» неделю. Админу нужно видеть не только
    что кто-то отстал, но и кто именно."""
    c1 = make_active_client(name="Ксюша", tg_id=7010)
    c2 = make_active_client(name="Дима", tg_id=7011)
    a = services.add_device(c1.id, "Телефон")
    b = services.add_device(c2.id, "Ноут")
    _seen(services, a.device_id, ago_days=1)
    _seen(services, b.device_id, ago_days=1)
    services.migration_start()
    _seen(services, _twin(services, a.device_id).id, ago_days=0)

    assert services.migration_pending() == [("Дима", "Ноут")]


# ── выходы ───────────────────────────────────────────────────────────────────

def test_cancel_keeps_moved_peers_alive(services, mig, make_active_client):
    """Отмена НЕ роняет коннекты переехавших: люди на них сидят прямо сейчас, и
    рвать связь ради отката — ровно тот вред, которого отмена должна избежать."""
    c = make_active_client(name="c", tg_id=7012)
    dc = services.add_device(c.id, "Тел")
    _seen(services, dc.device_id, ago_days=1)
    services.migration_start()
    twin = _twin(services, dc.device_id)
    _seen(services, twin.id, ago_days=0)

    moved = services.migration_cancel()
    assert moved == 1
    assert not services.migration_running()
    assert services.db.cohort_ids() == set()
    assert twin.public_key in mig.peers_by_iface["awg1"], "коннект переехавшего оборван"
    assert services.db.get_device(dc.device_id) is not None, "старый пир не вернулся"
    assert [d.id for d in services.migration_orphan_twins()] == [twin.id]


def test_finish_drops_the_stragglers_and_merges_history(services, mig,
                                                        make_active_client):
    """Завершение снимает старые пиры, сливает потребление и называет уронённых.

    Слияние обязательно: в окне трафик размазан по паре строк, и удаление старой
    без него унесло бы половину месяца — молча и в пользу нарушителя лимита.
    """
    c = make_active_client(name="Ксюша", tg_id=7013)
    moved_dev = services.add_device(c.id, "Переехал")
    stuck = services.add_device(c.id, "Отстал")
    _seen(services, moved_dev.device_id, ago_days=1)
    _seen(services, stuck.device_id, ago_days=1)
    services.db.add_traffic_bulk([(moved_dev.device_id, 700, 300)])
    services.migration_start()

    twin = _twin(services, moved_dev.device_id)
    _seen(services, twin.id, ago_days=0)
    services.db.add_traffic_bulk([(twin.id, 100, 50)])

    removed, dropped, failed = services.migration_finish()
    assert removed == 2 and failed == []
    assert dropped == ["Ксюша — Отстал"], "уронённые названы не поимённо"
    assert services.db.get_device(moved_dev.device_id) is None
    assert services.db.get_device(twin.id).traffic_rx_month == 800, "история потеряна"
    assert services.db.get_device(twin.id).twin_of is None, "пара не расцеплена"
    assert not services.migration_running()


def test_finish_merges_rf_traffic_of_the_pair(services, mig, make_active_client):
    """РФ-часть потребления сливается вместе с обычной: в окне переезда она размазана по паре, и удаление старой
    строки без слияния унесло бы из разбивки половину месяца."""
    c = make_active_client(name="c", tg_id=7015)
    dc = services.add_device(c.id, "Тел")
    _seen(services, dc.device_id, ago_days=1)
    services.db.add_traffic_bulk([(dc.device_id, 700, 300)])
    services.db.rf_add_bulk([(dc.device_id, 70, 30)])
    services.migration_start()

    twin = _twin(services, dc.device_id)
    _seen(services, twin.id, ago_days=0)
    services.db.rf_add_bulk([(twin.id, 5, 6)])

    services.migration_finish()
    kept = services.db.get_device(twin.id)
    assert (kept.rf_rx_month, kept.rf_tx_month) == (75, 36), "РФ старой строки потерян при слиянии"
    assert kept.traffic_rx_month == 700, "обычное потребление при слиянии сдвинулось"


def test_finish_archives_rather_than_deletes_silently(services, mig, make_active_client):
    """Строки уходят в архив с явной причиной: иначе потом не восстановить, кто
    отвалился и почему."""
    c = make_active_client(name="c", tg_id=7014)
    dc = services.add_device(c.id, "Тел")
    _seen(services, dc.device_id, ago_days=1)
    services.migration_start()
    services.migration_finish()

    rows = services.db._connection().execute(
        "SELECT close_reason FROM devices_histories").fetchall()
    assert any(r["close_reason"] == "миграция" for r in rows)
