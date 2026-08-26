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

    removed, dropped, failed = services.migration_finish()
    assert removed == 2 and failed == []
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


# ── видимость и уведомление ──────────────────────────────────────────────────

def test_user_sees_one_row_per_device(services, mig, make_active_client):
    """В окне у устройства две строки, а человек обязан видеть одну.

    Показать обе значит показать шесть устройств вместо трёх, а выдать конфиг
    не той — вручить пира уходящего интерфейса. Правило живёт в одном месте:
    пропусти вызывающего — и он покажет лишнее или выдаст не то.
    """
    c = make_active_client(name="c", tg_id=7030)
    a = services.add_device(c.id, "A")
    b = services.add_device(c.id, "B")
    services.migration_start()

    visible = services.db.list_devices(c.id)
    assert len(visible) == 2, "видно обе строки пары"
    assert all(d.iface == "awg1" for d in visible), "показан уходящий интерфейс"
    assert len(services.db.list_devices(c.id, all_rows=True)) == 4
    assert services.db.count_devices(c.id) == 2, "лимит упрётся вдвое раньше срока"


def test_after_cancel_the_old_rows_come_back(services, mig, make_active_client):
    """Отмена возвращает людей на старые пиры — значит и в списке снова старые.
    Двойники при этом живы, но человеку их видеть незачем."""
    c = make_active_client(name="c", tg_id=7031)
    services.add_device(c.id, "A")
    services.migration_start()
    assert services.db.list_devices(c.id)[0].iface == "awg1"

    services.migration_cancel()
    visible = services.db.list_devices(c.id)
    assert len(visible) == 1 and visible[0].iface == ""
    assert len(services.db.list_devices(c.id, all_rows=True)) == 2, "двойник снесён"


def test_after_finish_only_the_new_row_remains(services, mig, make_active_client):
    """После завершения пар нет вовсе, и правило видимости вырождается."""
    c = make_active_client(name="c", tg_id=7032)
    dc = services.add_device(c.id, "A")
    _seen(services, dc.device_id, ago_days=1)
    services.migration_start()
    _seen(services, _twin(services, dc.device_id).id, ago_days=0)
    services.migration_finish()

    visible = services.db.list_devices(c.id)
    assert len(visible) == 1 and visible[0].iface == "awg1"
    assert visible[0].twin_of is None


def test_traffic_limit_sums_the_pair(services, mig, make_active_client):
    """Потребление в окне размазано по паре: лимит по одной строке дал бы
    человеку двойную квоту, а в интерфейсе показал бы обнулившийся расход."""
    c = make_active_client(name="c", tg_id=7033)
    dc = services.add_device(c.id, "A")
    services.migration_start()
    twin = _twin(services, dc.device_id)
    services.db.add_traffic(dc.device_id, 600, 0)
    services.db.add_traffic(twin.id, 400, 0)

    rows = services.db.list_devices(c.id, all_rows=True)
    assert sum(d.traffic_rx_month for d in rows) == 1000


def test_ready_is_announced_once_per_migration(services, mig, make_active_client):
    """Доклад на СМЕНУ состояния, не на тик: иначе «готово» приходило бы каждые
    три минуты. А следующий переезд обязан доложить о своей готовности сам."""
    c = make_active_client(name="c", tg_id=7034)
    dc = services.add_device(c.id, "A")
    _seen(services, dc.device_id, ago_days=1)
    services.migration_start()
    assert services.migration_ready_alerts() == [], "доложили до переезда"

    _seen(services, _twin(services, dc.device_id).id, ago_days=0)
    notes = services.migration_ready_alerts()
    assert len(notes) == 1 and "переехали" in notes[0].text
    assert services.migration_ready_alerts() == [], "доклад повторился"

    services.migration_cancel()
    services.migration_start()
    _seen(services, _twin(services, dc.device_id).id, ago_days=0)
    assert len(services.migration_ready_alerts()) == 1, "следующий переезд промолчал"


def test_empty_cohort_does_not_announce(services, mig, make_active_client):
    """Живых пиров не было вовсе — переезжать некому, и «все переехали» было бы
    сообщением ни о чём."""
    c = make_active_client(name="c", tg_id=7035)
    services.add_device(c.id, "Мёртвое")
    services.migration_start()
    assert services.migration_ready_alerts() == []


# ── исправления по тотальному ревью ──────────────────────────────────────────

def test_dangling_twin_survives_finish_visible(services, mig, make_active_client):
    """Старую строку пары удалила сверка ещё в окне — двойник с висячей ссылкой
    обязан пережить завершение видимым.

    Раньше _finish_plan такие пары пропускал, twin_of не обнулялся, а фильтр
    «twin_of IS NULL» после выключения рычага прятал устройство из ВСЕХ списков
    навсегда: пир работает, человек подключён, а устройства нет ни у кого.
    """
    c = make_active_client(name="c", tg_id=7040)
    dc = services.add_device(c.id, "Тел")
    _seen(services, dc.device_id, ago_days=1)
    services.migration_start()
    twin = _twin(services, dc.device_id)
    _seen(services, twin.id, ago_days=0)
    services.db.delete_device(dc.device_id, archive_reason=None)  # «сверка удалила»

    services.migration_finish()
    visible = services.db.list_devices(c.id)
    assert [d.id for d in visible] == [twin.id], "двойник пропал из списка"
    assert services.db.get_device(twin.id).twin_of is None, "висячая ссылка осталась"


def test_dangling_twin_visible_even_without_finish(services, mig, make_active_client):
    """Тот же случай, но рычаг выключен отменой: битая ссылка читается как
    «пары нет», а не как «прятать вечно»."""
    c = make_active_client(name="c", tg_id=7041)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    twin = _twin(services, dc.device_id)
    services.db.delete_device(dc.device_id, archive_reason=None)
    services.migration_cancel()

    assert [d.id for d in services.db.list_devices(c.id)] == [twin.id]


def test_device_limit_counts_the_pair_as_one(services, mig, make_active_client):
    """Лимит устройства в окне — СУММА по паре против лимита пары.

    Проверка каждой строки против её собственного лимита давала двойную квоту,
    у которой ни одна половина не дотягивает до порога: 30+30 при лимите 50
    не блокировали никого.
    """
    from awgbot.core.blocks import DeviceBlock as DB_
    GB = 1024 ** 3
    c = make_active_client(name="c", tg_id=7042)
    dc = services.add_device(c.id, "Тел", traffic_limit=50 * GB)
    services.migration_start()
    twin = _twin(services, dc.device_id)
    services.db.add_traffic(dc.device_id, 30 * GB, 0)
    services.db.add_traffic(twin.id, 30 * GB, 0)

    services.check_traffic_limits()
    assert int(services.db.get_device(twin.id).block_reason) & int(DB_.TRAFFIC_USER), \
        "пара выпила 60 из 50 и не заблокирована"
    assert int(services.db.get_device(dc.device_id).block_reason) & int(DB_.TRAFFIC_USER), \
        "блокировка не парная"


def test_routing_toggle_is_pairwise(services, mig, make_active_client, fake_routing):
    """Человек щёлкает видимого двойника, а его реальный трафик до переимпорта
    идёт со СТАРОГО адреса. Непарный тумблер не делал ничего: выключение не
    выключало, включение не включало — молча."""
    c = make_active_client(name="c", tg_id=7043)
    dc = services.add_device(c.id, "Тел")
    services.set_routing_allowed(c.id, True)
    services.set_routing_device(dc.device_id, True)
    services.migration_start()
    twin = _twin(services, dc.device_id)

    services.set_routing_device(twin.id, False)          # щёлкнули видимого
    assert services.db.get_device(dc.device_id).routing_on == 0, \
        "старая строка осталась включённой — реальный трафик всё ещё метится"

    services.set_routing_device(dc.device_id, True)      # и в обратную сторону
    assert services.db.get_device(twin.id).routing_on == 1


def test_friend_sees_the_device_once(services, mig, make_active_client):
    """Активная связь копируется на обе строки пары, и без фильтра друг видел
    устройство дважды — и мог вытащить конфиг уходящего интерфейса."""
    c = make_active_client(name="c", tg_id=7044)
    dc = services.add_device(c.id, "Общее")
    code = services.make_device_friendly(dc.device_id)
    services.activate_friend(code, tg_id=7099)
    services.migration_start()

    rows = services.db.get_devices_by_friend_tg(7099)
    assert len(rows) == 1, "друг видит устройство дважды"
    assert rows[0].iface == "awg1", "другу показан уходящий интерфейс"

    services.migration_cancel()
    rows = services.db.get_devices_by_friend_tg(7099)
    assert len(rows) == 1 and rows[0].iface == "", "после отмены друг не вернулся на старую"


def test_reassign_moves_the_pair(services, mig, make_active_client):
    """Перенос одной строки разрывал пару между профилями: старый пир оставался
    у донора, а завершение слило бы трафик и заархивировало устройство не тому."""
    c1 = make_active_client(name="Донор", tg_id=7045)
    c2 = make_active_client(name="Получатель", tg_id=7046, device_limit=5)
    dc = services.add_device(c1.id, "Тел")
    services.migration_start()
    twin = _twin(services, dc.device_id)

    services.reassign_device(twin.id, c2.id)
    assert services.db.get_device(dc.device_id).client_id == c2.id, \
        "старая строка пары осталась у донора"
    assert services.db.get_device(twin.id).client_id == c2.id


def test_finish_keeps_running_when_server_refuses(services, mig, make_active_client,
                                                  monkeypatch):
    """Снятие пира не удалось — пару не трогаем ВОВСЕ и рычаг не гасим.

    Прежний порядок сливал историю и удалял строку до снятия: пир оставался в
    конфиге без строки в БД (следующая сверка — карантин с тревогой), а
    повторное завершение слило бы трафик дважды.
    """
    from awgbot.infra import awg as infra_awg
    c = make_active_client(name="c", tg_id=7047)
    dc = services.add_device(c.id, "Тел")
    _seen(services, dc.device_id, ago_days=1)
    services.db.add_traffic(dc.device_id, 700, 0)
    services.migration_start()
    twin = _twin(services, dc.device_id)
    _seen(services, twin.id, ago_days=0)

    def refuse(pub, iface=None):
        raise infra_awg.AwgError("нет связи")
    monkeypatch.setattr(infra_awg, "remove_peer", refuse)

    removed, dropped, failed = services.migration_finish()
    assert removed == 0 and failed == ["Тел"]
    assert services.migration_running(), "рычаг погашен при живых старых пирах"
    assert services.db.get_device(dc.device_id) is not None, "строка удалена без снятия пира"
    assert services.db.get_device(twin.id).traffic_rx_month == 0, "история слита до снятия"

    monkeypatch.setattr(infra_awg, "remove_peer",
                        lambda pub, iface=None: mig.removed.append((pub, iface)))
    removed, _, failed = services.migration_finish()
    assert removed == 1 and failed == []
    assert services.db.get_device(twin.id).traffic_rx_month == 700, \
        "история слита не один раз либо потеряна"
    assert not services.migration_running()


def test_start_aborts_when_nothing_is_born(services, mig, make_active_client,
                                           monkeypatch):
    """awg1 ещё не поднят: ни один двойник не родился — рычаг НЕ включается.

    Включиться в этом состоянии значило бы молча фоллбэчить всю выдачу на
    старые конфиги при формально идущем переезде.
    """
    from awgbot.infra import awg as infra_awg
    c = make_active_client(name="c", tg_id=7048)
    dc = services.add_device(c.id, "Тел")
    _seen(services, dc.device_id, ago_days=1)

    def down(pub, psk, ip, iface=None):
        raise infra_awg.AwgError("интерфейс не поднят")
    monkeypatch.setattr(infra_awg, "add_peer", down)

    res = services.migration_start()
    assert res.started is False and res.failed == ["Тел"]
    assert not services.migration_running(), "рычаг включился без единого двойника"
    assert services.db.cohort_ids() == set(), "когорта осталась замороженной"


def test_cancel_returns_pending_invite_to_the_old_row(services, mig,
                                                      make_active_client):
    """Код при рождении переносили на двойника — отмена обязана перенести его
    обратно: двойник прячется, и активация присланной ссылки включила бы
    невидимую строку."""
    c = make_active_client(name="c", tg_id=7049)
    dc = services.add_device(c.id, "Тел")
    code = services.make_device_friendly(dc.device_id)
    services.migration_start()
    assert services.db.get_device(dc.device_id).friend_code is None  # переносился

    services.migration_cancel()
    assert services.db.get_device(dc.device_id).friend_code == code
    assert services.db.get_device_by_friend_code(code).id == dc.device_id


def test_rename_is_pairwise(services, mig, make_active_client):
    """Иначе отмена вернула бы старую строку со старым именем — переименование
    молча откатилось бы."""
    c = make_active_client(name="c", tg_id=7050)
    dc = services.add_device(c.id, "Старое имя")
    services.migration_start()
    twin = _twin(services, dc.device_id)
    services.rename_device(twin.id, "Новое имя")

    services.migration_cancel()
    assert services.db.list_devices(c.id)[0].name == "Новое имя"


def test_visibility_honours_the_config_kill_switch(services, mig, make_active_client,
                                                   monkeypatch):
    """Очистка ключей в app.yaml — аварийный рубильник. Пока фильтр читал сырое
    состояние, механика выключалась, а видимость нет: людям показывались
    двойники, чьи конфиги больше не выдаются."""
    c = make_active_client(name="c", tg_id=7051)
    services.add_device(c.id, "Тел")
    services.migration_start()
    assert services.db.list_devices(c.id)[0].iface == "awg1"

    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "")
    assert services.db.list_devices(c.id)[0].iface == "", \
        "рубильник выключил механику, но не видимость"


def test_iface_normalizes_after_the_default_flip(services, mig, make_active_client,
                                                 monkeypatch):
    """Эндшпиль: админ переключил дефолт на новый интерфейс — явные метки
    схлопываются в пустую строку. Две записи одного смысла — класс расхождения,
    который уже стрелял."""
    c = make_active_client(name="c", tg_id=7052)
    dc = services.add_device(c.id, "Тел")
    _seen(services, dc.device_id, ago_days=1)
    services.migration_start()
    twin = _twin(services, dc.device_id)
    _seen(services, twin.id, ago_days=0)
    services.migration_finish()

    monkeypatch.setattr(config, "AWG_INTERFACE", "awg1")   # «переключил app.yaml»
    n = services.db.normalize_default_iface(config.AWG_INTERFACE)
    assert n == 1
    assert services.db.get_device(twin.id).iface == ""


async def test_stale_finish_button_is_refused(services, mig, make_active_client,
                                              fake_bot):
    """«finish!» из старого сообщения в истории чата, нажатый после отмены, снёс
    бы старые пиры орфанов и заархивировал ровно то, что отмена сохранила."""
    from awgbot.bot.handlers import settings as sh
    from awgbot.bot.callbacks import SetCB
    from tests.conftest import FakeCallback, FakeMessage

    c = make_active_client(name="c", tg_id=7053)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    twin = _twin(services, dc.device_id)
    _seen(services, twin.id, ago_days=0)
    services.migration_cancel()                          # орфан остался жить

    cb = FakeCallback(message=FakeMessage(chat_id=1, user_id=1, bot=fake_bot),
                      user_id=1, bot=fake_bot)
    await sh.migration_action(cb, SetCB(sec="mig", act="do", key="finish!"), services)
    assert services.db.get_device(dc.device_id) is not None, \
        "устаревшая кнопка выполнила завершение"
    assert twin.public_key in mig.peers_by_iface["awg1"]


def test_marking_hook_covers_every_client_subnet(monkeypatch):
    """Хук маркировки ставится на КАЖДУЮ клиентскую подсеть.

    Сужённый одной, он оставлял переехавших вообще без маркировки — и отказ был
    молчаливым и обманчивым: туннель работает, DNS отвечает, интернет есть, а
    российские сервисы видят зарубежный адрес. То есть ровно то, ради чего
    функция существует, не работает, и связать это с переездом неоткуда.
    """
    from awgbot.infra import routing as rt
    monkeypatch.setattr(config, "ROUTING_GW_INTERFACE", "awglink")
    monkeypatch.setattr(config, "ROUTING_CLIENT_SUBNET", "10.8.1.0/24")
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg0")
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "awg1")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "10.9.1")

    present: set = set()
    added: list = []

    def host_ok(args):
        if args[:2] == ["iptables", "-t"] and "-C" in args:
            return " ".join(args) in present
        return True

    def mangle(args, check=True):
        line = " ".join(["iptables", "-t", "mangle", "-C"] + args[1:])
        if args[0] == "-I":
            present.add(line); added.append(args)
        elif args[0] == "-D":
            present.discard(line)

    monkeypatch.setattr(rt, "_host_ok", host_ok)
    monkeypatch.setattr(rt, "_mangle", mangle)
    monkeypatch.setattr(rt, "ensure_policy", lambda: None)

    rt.set_marking_enabled(True)
    subnets = {a[a.index("-s") + 1] for a in added}
    assert subnets == {"10.8.1.0/24", "10.9.1.0/24"}, \
        "переехавшие остались без маркировки"
    assert rt._hook_present() is True

    rt.set_marking_enabled(False)
    assert rt._hook_present() is False
    assert not present, "часть хуков осталась висеть после снятия рубильника"


def test_partial_hook_set_counts_as_absent(monkeypatch):
    """Один хук стоит, второй нет — это «не включено», а не «включено».

    Иначе рубильник показывал бы рабочее состояние, пока половина людей идёт
    мимо маркировки.
    """
    from awgbot.infra import routing as rt
    monkeypatch.setattr(config, "ROUTING_CLIENT_SUBNET", "10.8.1.0/24")
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg0")
    monkeypatch.setattr(config, "MIGRATION_INTERFACE", "awg1")
    monkeypatch.setattr(config, "MIGRATION_SUBNET_PREFIX", "10.9.1")
    monkeypatch.setattr(rt, "_host_ok",
                        lambda args: "10.8.1.0/24" in " ".join(args))
    assert rt._hook_present() is False


def test_device_counter_shows_visible_rows(services, mig, make_active_client):
    """«Включено на N из M» считается по видимым строкам: в окне переезда у
    каждого устройства их две, и сырой счёт показывал человеку удвоенное."""
    c = make_active_client(name="c", tg_id=7060)
    a = services.add_device(c.id, "A")
    services.add_device(c.id, "B")
    services.set_routing_allowed(c.id, True)
    services.set_routing_device(a.device_id, True)
    services.migration_start()

    assert services.routing_device_counts(c.id) == (1, 2)
    assert len(services.routing_devices(c.id)) == 2


# ── поздравление пользователю ────────────────────────────────────────────────

def _connect(services, monkeypatch, device, ago_days=0):
    """Провести хендшейк ЧЕРЕЗ ОПРОС — так, как это происходит в бою.

    Не через прямую запись в БД: поздравление рождается на переходе «хендшейка
    не было → есть», который видит именно опрос. Подмени запись напрямую — и
    проверять будет нечего.
    """
    from awgbot.util import timeutil
    ts = int(timeutil.now().timestamp()) - int(ago_days * 86400)
    dev = services.db.get_device(device if isinstance(device, int) else device.id)
    monkeypatch.setattr(
        infra_awg, "show_dump",
        lambda iface=None: ([{"public_key": dev.public_key, "rx": 1, "tx": 1,
                              "last_handshake": ts}]
                            if iface == dev.iface or (iface == "awg0" and not dev.iface)
                            else []))
    return services.poll_traffic()


def test_user_is_greeted_the_moment_the_device_connects(services, mig, monkeypatch,
                                                        make_active_client):
    """Поздравление рождается в момент, когда опрос увидел первый хендшейк.

    Не по расписанию обхода: тот и опаздывал бы, и перепроверял бы всех впустую
    до конца переезда. Переход «было пусто → стало значение» случается один раз
    по определению, поэтому и хранить отметки не нужно.
    """
    c = make_active_client(name="c", tg_id=7070)
    dc = services.add_device(c.id, "Ноут")
    services.migration_start()
    twin = _twin(services, dc.device_id)

    notes = _connect(services, monkeypatch, twin)
    assert len(notes) == 1 and notes[0].tg_id == 7070
    assert "Ноут" in notes[0].text and "Всё получилось" in notes[0].text

    assert _connect(services, monkeypatch, twin) == [], "поздравление повторилось"


def test_greeting_is_silent_in_quiet_hours(services, mig, monkeypatch,
                                           make_active_client):
    """Не помечено громким — значит в тихие часы уйдёт беззвучно. Событие
    приятное, но реакции не требует: будить ради него незачем."""
    c = make_active_client(name="c", tg_id=7071)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    notes = _connect(services, monkeypatch, _twin(services, dc.device_id))
    assert notes[0].force_sound is False


def test_greeting_goes_to_the_friend_not_the_owner(services, mig, monkeypatch,
                                                   make_active_client):
    """Для расшаренного устройства адресат — ДРУГ: конфиг в приложении у него,
    и просьба удалить старый профиль осмысленна только для него."""
    c = make_active_client(name="Хозяин", tg_id=7072)
    dc = services.add_device(c.id, "Общее")
    code = services.make_device_friendly(dc.device_id)
    services.activate_friend(code, tg_id=7099)
    services.migration_start()

    notes = _connect(services, monkeypatch, _twin(services, dc.device_id))
    assert len(notes) == 1 and notes[0].tg_id == 7099, "поздравили не того"


def test_old_device_connecting_is_not_greeted(services, mig, monkeypatch,
                                              make_active_client):
    """Подключение на СТАРОМ пире — не переезд. Человек просто пользуется тем,
    что у него было, и поздравлять его не с чем."""
    c = make_active_client(name="c", tg_id=7073)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    assert _connect(services, monkeypatch, dc.device_id) == []


def test_no_greeting_when_migration_is_not_running(services, mig, monkeypatch,
                                                   make_active_client):
    """Отмена прошла — поздравлять не с чем: выдаются снова старые конфиги, и
    двойник, на котором кто-то подключился позже, это не переезд."""
    c = make_active_client(name="c", tg_id=7074)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    twin = _twin(services, dc.device_id)
    services.migration_cancel()
    assert _connect(services, monkeypatch, twin) == []
