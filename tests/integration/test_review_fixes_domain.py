"""Правки по ревью домена основной роли: учёт трафика, фиды подсетей сервисов,
ключи устройств, подписки.

- опрос трафика сериализован: дамп и сравнение с сэмплом — под одним замком,
  второй опрос (кнопка «🔄», передача резерву, гость) снимает дамп только после
  записи первого;
- фиды подсетей сервисов для шлюзов: сбор, где часть источников не ответила, не
  публикуется поверх прежнего фида — прежний остаётся, повтор через полчаса; без
  прежнего неполный лучше пустого, но повтор тоже скорый;
- перевыпуск ключей устройства в окне переезда: у самого и у двойника — свои пары
  ключей; отказ сервера на втором пире возвращает прежние ключи первому;
- «✏️ Период» на открытой админ-паузе правит сохранённый конец подписки, а не
  period_end (он на паузе пуст — подписка временно бессрочная);
- такт живости шлюзов берёт активный слот из того же прочитанного состава слотов.

Цена ошибки: дамп, снятый раньше, записывался после более позднего — дельта
выходила отрицательной, принималась за рестарт awg, и весь счётчик интерфейса
(десятки гигабайт с подъёма) уходил в трафик месяца — лимит срабатывал зря;
неполный фид уходил шлюзам как полный — подсети Telegram или Google выпадали из
VPN в локальной сети до следующего похода (до восьми часов); одна пара ключей на
оба пира — IntegrityError по public_key посреди перевыпуска: на сервере у
двойника уже новый ключ, в базе прежний, перепривязка друга к другому владельцу
в окне переезда не проходит, а сверка видит «чужой пир»; правка дат на паузе
писала period_end, выход из паузы затирал его сохранённым концом — админ ставил
пользователю двести дней, а после снятия блока у того оставался прежний месяц;
активный из второго чтения состава — KeyError посреди такта живости при первом
шлюзе или появлении слота 29: такт срывался, переключение и объявления ждали.
"""
from __future__ import annotations

import datetime
import threading
import time

import pytest

from awgbot.infra import awg
from awgbot.core.blocks import ClientBlock
from awgbot.domain.services.types import ServiceError
from awgbot.util import timeutil
from tests.integration._migration_common import _twin
from tests.integration.test_gwlink_lists_role_diag import ITDOG, _Net

pytestmark = pytest.mark.integration


# ── опрос трафика: дамп и запись под одним замком ────────────────────────────

def test_poll_traffic_second_dump_waits_for_first_commit(services, make_active_client, monkeypatch):
    """Первый опрос снял дамп и задержался — второй не снимает свой, пока первый
    не записал сэмпл; итог месяца — ровно прирост, без «рестарта»."""
    c = make_active_client(name="a", tg_id=7001)
    d = services.add_device(c.id, "Тел")
    pub = services.db.get_device(d.device_id).public_key
    base = 10 * 2**30
    rx = {"v": base}
    first_dumped, release_first = threading.Event(), threading.Event()
    calls: list[int] = []

    def dump(name=None):
        v = rx["v"]
        calls.append(v)
        if len(calls) == 2:              # дамп опроса A — снят, но запись задержана
            first_dumped.set()
            assert release_first.wait(5)
        return [dict(public_key=pub, rx=v, tx=0, last_handshake=None, allowed_ips="10.8.1.2/32")]

    monkeypatch.setattr(awg, "show_dump", dump)
    monkeypatch.setattr(awg, "read_file", lambda p: "")
    services.poll_traffic()                                  # первая база
    rx["v"] = base + 1000
    a = threading.Thread(target=services.poll_traffic)
    a.start()
    assert first_dumped.wait(5)
    rx["v"] = base + 2000                                    # опрос B пришёл позже A
    b = threading.Thread(target=services.poll_traffic)
    b.start()
    b.join(0.3)
    assert len(calls) == 2, "второй опрос снял дамп до записи первого"
    release_first.set()
    a.join(5)
    b.join(5)
    assert calls == [base, base + 1000, base + 2000]
    assert services.db.get_device(d.device_id).traffic_rx_month == 2000


# ── фиды подсетей: неполный сбор не заменяет прежний ─────────────────────────

@pytest.fixture()
def lan_slot(services, make_active_client):
    """Слот 1 с режимом «за шлюзом — без VPN»: фиды кому-то нужны."""
    admin = make_active_client(name="Админ", tg_id=7100, device_limit=0)
    pi = services.add_device(admin.id, "Шлюз")
    services.db.gateway_add(pi.device_id, "awglink", 443, "127.0.0.0/30", slot_id=1)
    services.gateway_set_lan_subnets(1, "192.168.68.0/24")
    services.gateway_set_lan_mode(1, True)
    return services


@pytest.mark.parametrize("missing", [f"{ITDOG}/Subnets/IPv4/telegram.lst",
                                     "https://www.gstatic.com/ipranges/goog.json"])
def test_partial_nets_keep_the_previous_feeds(lan_slot, monkeypatch, missing):
    """Список одного сервиса не пришёл, остальные — с новым содержимым: прежний
    фид остаётся целиком, сверка не засчитана, следующий поход — через полчаса."""
    services = lan_slot
    net = _Net(monkeypatch)
    assert services.gwlink_lan_feeds_update(force=True) is True
    before = services.gwlink_lan_feeds()
    checked = services.gwlink_lan_feeds_checked()
    net.body[f"{ITDOG}/Subnets/IPv4/meta.lst"] = "31.13.24.0/21\n157.240.0.0/16\n"   # новое у другого сервиса
    del net.body[missing]
    now = int(time.time())
    assert services.gwlink_lan_feeds_update(force=True) is False
    assert services.gwlink_lan_feeds() == before, "неполный сбор заменил фид — подсети сервиса ушли со шлюзов"
    assert services.gwlink_lan_feeds_checked() == checked, "неполный сбор засчитан как сверка"
    nxt = int(services.db.get_state(services._LAN_FEEDS_AT_KEY))
    assert nxt - now <= int(1800 * 1.4) + 2, f"повтор через {nxt - now} с, а не скоро"


def test_partial_nets_without_previous_feeds_are_published_and_retried_soon(lan_slot, monkeypatch):
    """Фидов нет вовсе (новый ВПС): неполный лучше пустого — публикуется, но
    повтор скорый и сверка не засчитана; полный следующий сбор заменяет его."""
    services = lan_slot
    net = _Net(monkeypatch)
    del net.body[f"{ITDOG}/Subnets/IPv4/telegram.lst"]
    now = int(time.time())
    assert services.gwlink_lan_feeds_update(force=True) is True
    assert "31.13.24.0/21" in services.gwlink_lan_feeds()["nets"]
    assert services.gwlink_lan_feeds_checked() == ""
    assert int(services.db.get_state(services._LAN_FEEDS_AT_KEY)) - now <= int(1800 * 1.4) + 2
    net.body[f"{ITDOG}/Subnets/IPv4/telegram.lst"] = "91.108.4.0/22\n"
    services.db.set_state(services._LAN_FEEDS_AT_KEY, str(now - 1))         # срок повтора настал
    assert services.gwlink_lan_feeds_update() is True, "полный сбор после неполного не заменил фид"
    assert "91.108.4.0/22" in services.gwlink_lan_feeds()["nets"]


# ── перевыпуск ключей в окне переезда ────────────────────────────────────────

def _held_pair(services, make_active_client):
    """Устройство отдано другу, переезд начат — у устройства двойник."""
    a = make_active_client(name="a", tg_id=7001)
    b = make_active_client(name="b", tg_id=7002)
    dc = services.add_device(a.id, "Тел")
    assert services.activate_friend(services.make_device_friendly(dc.device_id), 7003, "Друг").ok
    services.migration_start()
    twin = _twin(services, dc.device_id)
    assert twin is not None
    return dc.device_id, twin.id, b


def test_reassign_in_migration_window_rekeys_both_peers_separately(services, mig, make_active_client):
    """Устройство друга уходит другому владельцу в окне переезда: у обоих пиров
    новые и разные ключи, на сервере стоят ровно они, прежних нет."""
    orig_id, twin_id, b = _held_pair(services, make_active_client)
    old = {i: services.db.get_device(i).public_key for i in (orig_id, twin_id)}
    services.reassign_device(twin_id, b.id)
    d0, d1 = services.db.get_device(orig_id), services.db.get_device(twin_id)
    assert d0.client_id == d1.client_id == b.id
    assert d0.public_key != d1.public_key, "одна пара ключей на оба пира"
    assert d0.public_key != old[orig_id] and d1.public_key != old[twin_id], "ключи не перевыпущены"
    on_server = {pub for peers in mig.peers_by_iface.values() for pub in peers}
    assert {d0.public_key, d1.public_key} <= on_server
    assert not set(old.values()) & on_server, "прежний ключ остался на сервере — прежний держатель при доступе"


def test_rekey_failure_on_the_second_peer_restores_the_first(services, mig, make_active_client, monkeypatch):
    """Сервер отказал на втором пире пары: ServiceError, у обоих пиров — прежние
    ключи и в базе, и на сервере."""
    from awgbot.infra import awg as infra_awg
    orig_id, twin_id, _b = _held_pair(services, make_active_client)
    old = {i: services.db.get_device(i).public_key for i in (orig_id, twin_id)}
    twin_iface = infra_awg.iface_of(services.db.get_device(twin_id).iface)
    real_add = infra_awg.add_peer

    def add_peer(pub, psk, ip, iface=None):
        if infra_awg.iface_of(iface) == twin_iface and pub not in old.values():
            raise infra_awg.AwgError("отказ сервера")
        real_add(pub, psk, ip, iface=iface)

    monkeypatch.setattr(infra_awg, "add_peer", add_peer)
    with pytest.raises(ServiceError):
        services.rekey_device(orig_id)
    assert {i: services.db.get_device(i).public_key for i in (orig_id, twin_id)} == old
    on_server = {pub for peers in mig.peers_by_iface.values() for pub in peers}
    assert set(old.values()) <= on_server, "прежний ключ не вернулся на сервер"


# ── правка дат на открытой админ-паузе ───────────────────────────────────────

def test_period_edit_during_open_admin_pause_survives_unpause(services, make_active_client):
    """Блок без срока паузы, «✏️ Период» на двести дней, снятие блока: конец —
    заданный (плюс дни паузы, которые и так прибавляются), а не прежний месяц;
    на самой паузе подписка остаётся временно бессрочной."""
    c = make_active_client(name="a", tg_id=7001, period_kind="month")
    services.block_client_manual(c.id, ClientBlock.ADMIN_SILENT, notify=False, pause_days=0)
    assert services.db.get_client(c.id).pause_mode == "admin_open"
    want = timeutil.now() + datetime.timedelta(days=200)
    services.set_subscription_dates(c.id, timeutil.now(), want)
    mid = services.db.get_client(c.id)
    assert mid.period_end is None, "на открытой паузе period_end заполнен — пауза перестала быть бессрочной"
    assert mid.effective_period_end == timeutil.to_iso(want)
    services.unblock_client_manual(c.id, ClientBlock.ADMIN_SILENT, notify=False)
    end = timeutil.parse_iso(services.db.get_client(c.id).period_end)
    assert datetime.timedelta(0) <= end - want <= datetime.timedelta(days=1), (
        f"конец после паузы {end}, а задан {want}")


def test_period_edit_to_unlimited_during_open_admin_pause(services, make_active_client):
    """Бессрочная, заданная на паузе, после паузы бессрочной и остаётся."""
    c = make_active_client(name="a", tg_id=7001, period_kind="month")
    services.block_client_manual(c.id, ClientBlock.ADMIN_SILENT, notify=False, pause_days=0)
    services.set_subscription_dates(c.id, timeutil.now(), None)
    services.unblock_client_manual(c.id, ClientBlock.ADMIN_SILENT, notify=False)
    assert services.db.get_client(c.id).period_end is None


# ── такт живости шлюзов: активный — из того же состава слотов ─────────────────

def test_liveness_tick_takes_the_active_slot_from_the_same_slot_list(services, fake_awg, fake_routing,
                                                                    make_active_client, monkeypatch):
    """Такт прочитал состав слотов, а между чтениями появился слот (первый шлюз,
    слот 29 «шлюзы второго сервера»), записанный активным: активный берётся из
    прочитанного состава — без KeyError на стрике хороших, такт доходит до конца."""
    import sys
    from awgbot.core import config as cfg
    from awgbot.infra import routing
    admin = make_active_client(name="Админ", tg_id=cfg.ADMIN_ID, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    pi2 = services.add_device(admin.id, "Pi2")
    g1 = services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    monkeypatch.setattr(services, "_probe_slot", lambda g, active=False: "ok")
    monkeypatch.setattr(services, "_rt_standby_interval", lambda: 0)
    monkeypatch.setattr(routing.marking, "switch_active", lambda iface: None)
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: None)
    services.db.set_state(services._RT_ACTIVE_KEY, "2")
    real = type(services).rt_slots

    def rt_slots(self=services):
        if sys._getframe(1).f_code.co_name == "routing_liveness_tick":
            return [services.db.gateway(g1.id)]               # состав такта — до появления слота 2
        return real(services)
    monkeypatch.setattr(services, "rt_slots", rt_slots)
    assert isinstance(services.routing_liveness_tick(), list)   # KeyError здесь — дефект
