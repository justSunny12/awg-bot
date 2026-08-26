"""Переезд профилей на второй интерфейс (docs/ROADMAP.md, п.3).

Каркас: рождение двойников со всем наследованием, замороженная когорта, прогресс
и оба выхода. Инфраструктура подменена — проверяем, ЧТО проецируется наружу и
что остаётся в базе, а не как выглядит вывод awg.
"""
import pytest

from awgbot.core import config
from awgbot.core.blocks import DeviceBlock
from awgbot.core.enums import FriendStatus
from awgbot.domain import migration
from awgbot.infra import awg as infra_awg

pytestmark = pytest.mark.integration


@pytest.fixture()
def mig(monkeypatch, services, fake_awg):
    """Переезд настроен, второй интерфейс отвечает. Возвращает состояние фейка:
    .peers_by_iface — что реально ушло на сервер."""
    import types
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg0")
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "awg1")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "10.9.1")

    state = types.SimpleNamespace(peers_by_iface={}, removed=[], blocked=set())

    def add_peer(pub, psk, ip, iface=None):
        state.peers_by_iface.setdefault(infra_awg.iface_of(iface), {})[pub] = ip

    def remove_peer(pub, iface=None):
        state.removed.append((pub, infra_awg.iface_of(iface)))
        state.peers_by_iface.get(infra_awg.iface_of(iface), {}).pop(pub, None)

    monkeypatch.setattr(infra_awg, "add_peer", add_peer)
    monkeypatch.setattr(infra_awg, "remove_peer", remove_peer)
    monkeypatch.setattr(infra_awg, "read_occupied_ips", lambda iface=None: set())
    monkeypatch.setattr(infra_awg, "block_ip", lambda ip: state.blocked.add(ip))
    monkeypatch.setattr(infra_awg, "unblock_ip", lambda ip: state.blocked.discard(ip))
    return state


def _seen(services, device_id, ago_days=0):
    """Отметить хендшейк: «подключался ago_days назад»."""
    from awgbot.util import timeutil
    ts = int(timeutil.now().timestamp()) - int(ago_days * 86400)
    services.db.update_device_fields(device_id, last_handshake=ts)


def awg_iface(dev):
    return infra_awg.iface_of(dev.iface)


def _twin(services, old_id):
    return services.db.get_device(services.db.twins_by_origin()[old_id])


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
    services.db.set_sample(dc.device_id, 10 ** 9, 10 ** 9)

    services.migration_start()
    twin = _twin(services, dc.device_id)
    assert twin.missing_count == 0
    assert services.db.get_sample(twin.id) is None


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
    services.db.add_traffic(moved_dev.device_id, 700, 300)
    services.migration_start()

    twin = _twin(services, moved_dev.device_id)
    _seen(services, twin.id, ago_days=0)
    services.db.add_traffic(twin.id, 100, 50)

    removed, dropped = services.migration_finish()
    assert removed == 2
    assert dropped == ["Ксюша — Отстал"], "уронённые названы не поимённо"
    assert services.db.get_device(moved_dev.device_id) is None
    assert services.db.get_device(twin.id).traffic_rx_month == 800, "история потеряна"
    assert services.db.get_device(twin.id).twin_of is None, "пара не расцеплена"
    assert not services.migration_running()


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


# ── парные мутации и выдача ──────────────────────────────────────────────────

def test_config_is_generated_from_its_own_interface(services, mig, make_active_client,
                                                    monkeypatch):
    """Конфиг собирается по параметрам ТОГО интерфейса, где живёт пир.

    Общие параметры отдали бы двойнику старый порт, старый серверный ключ и
    старую обфускацию. Отказ молчаливый: превью выглядит нормально, человек
    импортирует — и не подключается никто.
    """
    asked: list = []
    orig = infra_awg.read_server_params

    def spy(force=False, iface=None):
        asked.append(infra_awg.iface_of(iface))
        params = orig(force=force, iface=iface)
        params["listen_port"] = 443 if infra_awg.iface_of(iface) == "awg1" else 42755
        return params

    monkeypatch.setattr(infra_awg, "read_server_params", spy)

    c = make_active_client(name="c", tg_id=7020)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    twin = _twin(services, dc.device_id)

    asked.clear()
    cfg = services.generate_config(twin.id)
    assert asked == ["awg1"], "параметры взяты у чужого интерфейса"
    assert "ListenPort" not in cfg["conf"] or "443" in cfg["conf"]

    asked.clear()
    services.generate_config(dc.device_id)
    assert asked == ["awg0"], "старому устройству подсунули новые параметры"


def test_delete_removes_both_peers(services, mig, make_active_client):
    """Удаление ПАРНОЕ. Снять только видимый пир значит оставить второй
    работать — призрачный доступ у того, кого человек считает удалённым."""
    c = make_active_client(name="c", tg_id=7021)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    twin = _twin(services, dc.device_id)

    services.remove_device(twin.id)
    assert mig.peers_by_iface.get("awg1", {}) == {}
    assert mig.peers_by_iface.get("awg0", {}) == {}, "старый пир остался жив"
    assert services.db.get_device(dc.device_id) is None
    assert services.db.get_device(twin.id) is None


def test_block_covers_both_peers(services, mig, make_active_client):
    """Заблокировать один пир, оставив второй, значит оставить человеку доступ:
    блокировка перестала бы что-либо значить ровно в окне переезда."""
    c = make_active_client(name="c", tg_id=7022)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    twin = _twin(services, dc.device_id)
    old = services.db.get_device(dc.device_id)

    services._device_set_block(dc.device_id, DeviceBlock.USER)
    assert {old.address, twin.address} <= mig.blocked
    assert int(services.db.get_device(twin.id).block_reason) == int(DeviceBlock.USER)

    services._device_clear_block(dc.device_id, DeviceBlock.USER)
    assert not ({old.address, twin.address} & mig.blocked), "DROP снят не со всех"
    assert int(services.db.get_device(twin.id).block_reason) == 0
