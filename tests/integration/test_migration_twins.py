"""Переезд профилей на второй интерфейс (docs/ROADMAP.md, п.3): рождение
двойников и наследование.

Двойники рождаются у всех, включая мёртвых; когорта замораживается и
переживает перезапуск; двойник наследует всё, что важно, и начинает с чистых
счётчиков. Инфраструктура подменена — проверяем, ЧТО проецируется наружу и
что остаётся в базе, а не как выглядит вывод awg.
"""
import pytest

from awgbot.core.blocks import DeviceBlock
from awgbot.core.enums import FriendStatus
from awgbot.infra import awg as infra_awg

from tests.integration._migration_common import _seen, _twin

pytestmark = pytest.mark.integration


def awg_iface(dev):
    return infra_awg.iface_of(dev.iface)


# ── рождение двойников ───────────────────────────────────────────────────────

def test_twins_are_born_for_everyone_including_the_dead(services, mig, make_active_client):
    """Двойники рождаются ВСЕМ, а не только живым.

    Переезд задуман бесшовным, отказаться от него нельзя. Оживший через месяц
    пир обязан найти себя на новом интерфейсе сам, без отдельного вмешательства
    админа — иначе «бесшовность» кончается ровно там, где она нужнее всего.
    """
    c = make_active_client(name="c", tg_id=7001)
    live = services.add_device(c.id, "Живое")
    dead = services.add_device(c.id, "Мёртвое")
    _seen(services, live.device_id, ago_days=1)

    res = services.migration_start()
    assert res.born == 2, "мёртвому двойник не достался"
    assert res.cohort_devices == 1, "мёртвое попало в когорту готовности"
    assert services.db.get_device(dead.device_id).twin_of is None, "мёртвому двойник не выдан"

    twin = _twin(services, live.device_id)
    assert twin.iface == "awg1"
    assert twin.address.startswith("10.9.1."), "двойник взял адрес из старой подсети"
    assert twin.public_key in mig.peers_by_iface["awg1"]


def test_quarantine_gets_no_twin(services, mig):
    """Карантинному пиру двойник не положен: приватного ключа у него нет,
    генерить конфиг нечем, и он вообще не наш."""
    service_id = services.db.get_service_client_id()
    services.db.create_device(service_id, "Чужой", "Q" * 43 + "=", "psk",
                              "10.8.1.99", private_key=None)
    res = services.migration_start()
    assert res.born == 0
    assert services.db.twins_by_origin() == {}


def test_start_is_idempotent(services, mig, make_active_client):
    """Сбой на середине лечится повторным включением.

    Рождение N двойников — это N отдельных операций, а не транзакция: сервер
    моргнул, адреса кончились — и часть осталась без пары. Повторный заход
    обязан докинуть недостающих и не задваивать уже созданных.
    """
    c = make_active_client(name="c", tg_id=7002)
    d1 = services.add_device(c.id, "A")
    d2 = services.add_device(c.id, "B")
    services.migration_start()
    # имитируем сбой на середине: двойник одного не доехал
    lost = services.db.twins_by_origin()[d2.device_id]
    services.db.delete_device(lost, archive_reason=None)

    res = services.migration_start()
    assert res.born == 1 and res.already == 1
    assert len(services.db.twins_by_origin()) == 2
    assert sum(1 for d in services.db.list_all_devices()
               if d.twin_of == d1.device_id) == 1, "двойник задвоился"


def test_device_created_during_the_window_gets_a_pair_too(services, mig,
                                                          make_active_client):
    """Устройство, заведённое уже в окне, заводится ПАРОЙ, как все остальные, а
    человеку выдаётся новый конфиг.

    Пара нужна не для симметрии: до завершения переезд можно отменить, а отмена
    возвращает людей на старые пиры. Роди мы только новый — отменять для этого
    человека было бы нечем, и он остался бы единственным, кого откат
    выбрасывает.
    """
    c = make_active_client(name="c", tg_id=7015)
    services.migration_start()
    fresh = services.add_device(c.id, "Новое")

    issued = services.db.get_device(fresh.device_id)
    assert issued.iface == "awg1", "выдан конфиг уходящего интерфейса"
    assert issued.address.startswith("10.9.1.")
    assert issued.twin_of is not None, "пары нет — отменять будет нечем"

    old = services.db.get_device(issued.twin_of)
    assert awg_iface(old) == "awg0" and old.name == "Новое"
    assert old.public_key in mig.peers_by_iface["awg0"], "старого пира нет на сервере"
    # повторный заход не должен родить ЕЩЁ одну пару
    assert services.migration_start().born == 0


def test_cohort_is_frozen_across_restarts(services, mig, make_active_client):
    """Когорта замораживается и НЕ пересобирается.

    Считай по живому правилу постоянно — и знаменатель гуляет: молчавший три
    недели пир проснулся, и 12/12 превращается в 12/13, а «завершено»
    становится состоянием, которое умеет расзавершаться.
    """
    c = make_active_client(name="c", tg_id=7003)
    live = services.add_device(c.id, "Живое")
    dead = services.add_device(c.id, "Мёртвое")
    _seen(services, live.device_id, ago_days=1)
    services.migration_start()
    assert services.db.cohort_ids() == {live.device_id}

    _seen(services, dead.device_id, ago_days=0)   # ожил уже в окне
    services.migration_start()                    # повторный заход (рестарт/добор)
    assert services.db.cohort_ids() == {live.device_id}, "когорта пересобрана"


def test_deleted_device_leaves_the_cohort(services, mig, make_active_client):
    """Заморозка защищает от оживания, но не от удаления: устройство, снесённое
    в окне, обязано выбыть, иначе x == y недостижимо и завершение не наступит
    никогда."""
    c = make_active_client(name="c", tg_id=7004)
    a = services.add_device(c.id, "A")
    b = services.add_device(c.id, "B")
    _seen(services, a.device_id, ago_days=1)
    _seen(services, b.device_id, ago_days=1)
    services.migration_start()
    assert len(services.db.cohort_ids()) == 2

    services.db.delete_device(a.device_id, archive_reason=None)
    assert services.db.cohort_ids() == {b.device_id}


# ── наследование ─────────────────────────────────────────────────────────────

def test_twin_inherits_everything_that_matters(services, mig, make_active_client):
    """Каждая неперенесённая строка — молчаливая регрессия, а не заметный отказ:
    человек подключается, всё «работает», и лишь потом выясняется, что режим
    выключился, лимит пропал или блокировка снялась."""
    c = make_active_client(name="c", tg_id=7005)
    dc = services.add_device(c.id, "Ноут", traffic_limit=50 * 1024 ** 3)
    services.set_routing_allowed(c.id, True)
    services.set_routing_device(dc.device_id, True)
    services._device_set_block(dc.device_id, DeviceBlock.USER)

    services.migration_start()
    twin = _twin(services, dc.device_id)
    assert twin.name == "Ноут"
    assert twin.client_id == c.id
    assert twin.traffic_limit == 50 * 1024 ** 3
    assert twin.routing_on == 1, "у всех молча выключился бы РФ-режим"
    assert int(twin.block_reason) == int(DeviceBlock.USER), "переезд стал амнистией"
    assert twin.address in mig.blocked, "DROP на новый адрес не наложен"


def test_twin_starts_with_clean_counters(services, mig, make_active_client):
    """missing_count и базовая точка дельт обязаны родиться чистыми: чужой
    счётчик пропаж уронил бы двойника сверкой, а чужая база записала бы весь
    его трафик одним скачком."""
    c = make_active_client(name="c", tg_id=7006)
    dc = services.add_device(c.id, "Тел")
    services.db.update_device_fields(dc.device_id, missing_count=2)
    services.db.set_samples([(dc.device_id, 10 ** 9, 10 ** 9)])

    services.migration_start()
    twin = _twin(services, dc.device_id)
    assert twin.missing_count == 0
    assert twin.id not in services.db.get_samples_all()


def test_active_friend_is_copied_pending_code_is_moved(services, mig, make_active_client):
    """Активная связь копируется — друг остаётся другом того же устройства.

    А невыбранный код ПЕРЕНОСИТСЯ: поиск по коду делает fetchone по неуникальной
    колонке, и с двумя строками под одним кодом активация попала бы в
    неопределённую из них. Отказ отложенный — всплыл бы, когда человек наконец
    нажмёт на присланную ссылку.
    """
    c = make_active_client(name="c", tg_id=7007)
    shared = services.add_device(c.id, "Общее")
    invited = services.add_device(c.id, "Приглашённое")
    code = services.make_device_friendly(shared.device_id)
    services.activate_friend(code, tg_id=7099)
    pending_code = services.make_device_friendly(invited.device_id)

    services.migration_start()

    twin_shared = _twin(services, shared.device_id)
    assert twin_shared.friend_tg_id == 7099
    assert twin_shared.friend_status == FriendStatus.ACTIVE
    assert services.db.get_device(shared.device_id).friend_tg_id == 7099

    twin_invited = _twin(services, invited.device_id)
    assert twin_invited.friend_code == pending_code
    assert services.db.get_device(invited.device_id).friend_code is None, \
        "код остался на обеих строках — активация попадёт в случайную"
    assert services.db.get_device_by_friend_code(pending_code).id == twin_invited.id
