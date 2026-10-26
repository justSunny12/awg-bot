"""РФ-доступ пользователей основы в гостевой зоне (domain/services/reserve/rfzone.py
и код маршрутизации основы): запасной слот «шлюзы резерва» у бота основы на
резерве — только в гостевом режиме и только пока резерв разрешил (`rf_via`
в состоянии от резерва), последний в порядке кандидатов, не липкий (слот
основы с тремя хорошими замерами забирает трафик обратно), живость — зондом
наружу через его таблицу; сторона резерва — `reserve_rf_via_view`.

Слоты основы — два (как в tests/integration/test_gateway_slots.py), зонды
по слоту — словарь `probe[slot]`; переключение линка — журнал `switched`.

Цена ошибки: запасной слот вне гостевого режима — основа на своём хосте
шлёт РФ-трафик в несуществующий veth, пользователи теряют РФ-доступ; без
разрешения резерва — пользователи основы ходят через шлюзы, которые админ
резерва закрыл; липкий запасной — РФ-адрес пользователей основы остаётся
адресом шлюза резерва после возврата её шлюза; запасной раньше слота
основы — пользователи уходят на чужой шлюз при живом своём; у основы РФ-доступ
выключен, а в зоне маркировка — пользователи, которым его не давали, получают его.
"""
from __future__ import annotations

import pytest

from awgbot.core import config, settings
from awgbot.domain.services.reserve import rfzone
from awgbot.infra import routing

pytestmark = pytest.mark.integration
ADMIN = config.ADMIN_ID
SPARE = rfzone.RF_SPARE_ID


@pytest.fixture()
def two(services, fake_awg, fake_routing, make_active_client, monkeypatch):
    """Два слота основы (реплика в зоне); зонды по слоту — probe[слот], у
    запасного — probe[30]."""
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    pi2 = services.add_device(admin.id, "Pi2")
    services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    probe = {1: "ok", 2: "ok", SPARE: "ok"}
    monkeypatch.setattr(services, "_probe_slot", lambda g, active=False: probe[g.id])
    monkeypatch.setattr(services, "_spare_verdict", lambda g, active: probe[g.id])
    monkeypatch.setattr(services, "_rt_standby_interval", lambda: 0)
    monkeypatch.setattr(services, "_rt_window_size", lambda: 10)
    switched = []
    monkeypatch.setattr(routing.marking, "switch_active", lambda iface: switched.append(iface))
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: None)
    services.probe, services.switched = probe, switched
    return services


def _guest(services, monkeypatch, *, rf_via: bool = True):
    monkeypatch.setattr(config, "GUEST", True)
    services.guest_state_in({"name": "Gamma", "rf_via": rf_via, "rf_cidr": "169.254.77.4/30"})


def _ticks(s, n: int) -> list:
    notes = []
    for _ in range(n):
        notes += s.routing_liveness_tick()
    return notes


def _settle(s):
    _ticks(s, s._RT_UP_STREAK)


def _both_down(s) -> list:
    s.probe[1] = s.probe[2] = "down"
    return _ticks(s, s._rt_fail_need())


# ── есть ли запасной слот ───────────────────────────────────────────────────

def test_outside_the_guest_mode_there_is_no_spare_even_with_rf_via(two, monkeypatch):
    two.guest_state_in({"name": "Gamma", "rf_via": True})
    assert two.guest_rf_spare() is None
    assert [g.id for g in two.rt_slots()] == [1, 2], "запасной слот у основы на своём хосте"


def test_without_the_reserves_permission_there_is_no_spare(two, monkeypatch):
    _guest(two, monkeypatch, rf_via=False)
    assert two.guest_rf_spare() is None and [g.id for g in two.rt_slots()] == [1, 2]
    two.guest_state_in({"name": "Gamma"})                     # прежний резерв без поля — нет разрешения
    assert two.guest_rf_spare() is None


def test_the_spare_is_the_last_candidate_and_named_after_the_reserve(two, monkeypatch):
    _guest(two, monkeypatch)
    slots = two.rt_slots()
    assert [g.id for g in slots] == [1, 2, SPARE], "запасной слот не последний"
    spare = slots[-1]
    assert (spare.link_if, spare.link_cidr, spare.device_id) == ("awggr-n", "169.254.77.4/30", 0)
    assert two._gw_name(spare) == "шлюзы «Gamma»" and two._gw_display(spare) == "шлюзы «Gamma»", \
        "имя запасного в уведомлениях — не по резерву (или с подписью дважды)"


def test_no_main_slots_means_no_spare_and_no_routing_slots(services, monkeypatch):
    """Шлюзов у основы нет — РФ-доступа у её пользователей не было и в зоне нет."""
    _guest(services, monkeypatch)
    assert services.rt_slots() == [] and services.active_gateway() is None


def test_the_spare_policy_is_laid_for_its_link_too(two, monkeypatch):
    _guest(two, monkeypatch)
    assert (SPARE, "awggr-n") in two.gateway_slots_policy(), "таблица и правило запасного слота не ставятся"


# ── переключение на запасной и обратно ──────────────────────────────────────

def test_both_main_slots_down_with_rf_via_moves_traffic_to_the_spare(two, monkeypatch):
    _guest(two, monkeypatch)
    _settle(two)
    assert two.active_gateway().id == 1
    notes = _both_down(two)
    assert two.active_gateway().id == SPARE and two.switched == ["awggr-n"], two.switched
    assert two.db.get_state(two._RT_LINK_KEY) == "1", "маркировка снялась, хотя запасной жив"
    assert any("шлюзы «Gamma»" in n.text for n in notes), [n.text for n in notes]


def test_one_main_slot_down_moves_to_the_other_main_slot_not_to_the_spare(two, monkeypatch):
    _guest(two, monkeypatch)
    _settle(two)
    two.probe[1] = "down"
    _ticks(two, two._rt_fail_need())
    assert two.active_gateway().id == 2 and two.switched == ["awglink2"], "запасной раньше живого слота основы"


def test_both_main_slots_down_without_rf_via_degrade_as_before(two, monkeypatch):
    _guest(two, monkeypatch, rf_via=False)
    _settle(two)
    _both_down(two)
    assert two.active_gateway().id == 1 and two.switched == []
    assert two.db.get_state(two._RT_LINK_KEY) == "0", "без разрешения резерва РФ-доступа нет"


def test_the_spare_is_not_sticky_a_main_slot_with_three_good_takes_traffic_back(two, monkeypatch):
    _guest(two, monkeypatch)
    _settle(two)
    _both_down(two)
    assert two.active_gateway().id == SPARE
    two.db.set_state(two._RT_SWITCHED_KEY, "")                # интервал переключений прошёл
    two.probe[1] = "ok"
    for i in range(two._RT_UP_STREAK - 1):
        two.routing_liveness_tick()
        assert two.active_gateway().id == SPARE, f"вернулись с {i + 1} хороших замеров"
    two.routing_liveness_tick()
    assert two.active_gateway().id == 1 and two.switched[-1] == "awglink", "запасной слот держит трафик (липкий)"
    assert two.db.get_state(two._RT_LINK_KEY) == "1"


def test_a_main_slot_coming_back_soon_after_the_switch_never_drops_rf_access(two, monkeypatch):
    """Слот основы ожил раньше интервала переключений: трафик может подождать
    на запасном, но РФ-доступ пользователей основы не снимается ни на такт и
    «второго переключения подряд» админу не сообщается — запасной жив."""
    _guest(two, monkeypatch)
    _settle(two)
    _both_down(two)
    assert two.active_gateway().id == SPARE
    two.probe[1] = "ok"
    notes = []
    for _ in range(two._RT_UP_STREAK + 2):
        notes += two.routing_liveness_tick()
        assert two.db.get_state(two._RT_LINK_KEY) == "1", "РФ-доступ снят, хотя запасной слот жив"
    assert not [n for n in notes if "Второе переключение" in n.text], [n.text for n in notes]


def test_the_permission_withdrawn_while_on_the_spare_leaves_the_spare(two, monkeypatch):
    _guest(two, monkeypatch)
    _settle(two)
    _both_down(two)
    assert two.active_gateway().id == SPARE
    two.guest_state_in({"name": "Gamma", "rf_via": False})
    assert two.active_gateway().id != SPARE, "разрешение снято, а трафик на шлюзах резерва"
    assert SPARE not in [g.id for g in two.rt_slots()]


# ── у основы РФ-доступ выключен ─────────────────────────────────────────────

def test_with_routing_off_at_the_primary_the_zone_marks_nothing(two, monkeypatch, fake_routing, make_active_client):
    _guest(two, monkeypatch)
    fake_routing.enabled = False
    c = make_active_client(name="Клиент", tg_id=7100)
    two.add_device(c.id, "Телефон")
    two.set_routing_allowed(c.id, True)
    two.reconcile_routing()
    _ticks(two, two._RT_UP_STREAK + 2)
    two.probe[1] = two.probe[2] = "down"
    _ticks(two, two._rt_fail_need())
    assert fake_routing.marking is not True, "маркировка в зоне при выключенном у основы РФ-доступе"
    assert "awggr-n" not in two.switched and two.db.get_state(two._RT_LINK_KEY) != "1"


# ── живость запасного: зонд наружу через его таблицу ────────────────────────

@pytest.fixture()
def spare(services, monkeypatch):
    _guest(services, monkeypatch)
    return services.guest_rf_spare()


def test_the_active_spare_is_probed_through_its_veth_and_cached_between_probes(services, spare, monkeypatch):
    calls = []
    part = next(m for m in routing._PARTS if "probe_gateway" in vars(m))
    monkeypatch.setattr(part, "probe_gateway", lambda targets, port, **kw: calls.append(kw) or "ok")
    monkeypatch.setattr(services, "_rt_idle_probe_interval", lambda: 300.0)
    assert services._spare_verdict(spare, True) == "ok"
    assert calls[0].get("iface") == "awggr-n" and "mark" not in calls[0], calls
    assert services._spare_verdict(spare, True) == "ok" and len(calls) == 1, "зонд каждый такт — маячок наружу"


def test_the_standby_spare_is_probed_with_its_mark_and_a_short_timeout(services, spare, monkeypatch):
    calls = []
    part = next(m for m in routing._PARTS if "probe_gateway" in vars(m))
    monkeypatch.setattr(part, "probe_gateway", lambda targets, port, **kw: calls.append(kw) or "down")
    monkeypatch.setattr(services, "_rt_standby_interval", lambda: 5)
    assert services._spare_verdict(spare, False) == "down"
    assert calls[0]["iface"] == "awggr-n" and calls[0]["mark"] == 1 << SPARE
    assert calls[0]["timeout"] == services._RT_STANDBY_TIMEOUT
    assert services._spare_verdict(spare, False) == "down" and len(calls) == 1, "резервный зонд без паузы"


def test_a_zero_interval_probes_the_spare_every_time(services, spare, monkeypatch):
    calls = []
    part = next(m for m in routing._PARTS if "probe_gateway" in vars(m))
    monkeypatch.setattr(part, "probe_gateway", lambda targets, port, **kw: calls.append(kw) or "ok")
    monkeypatch.setattr(services, "_rt_idle_probe_interval", lambda: 0)
    for _ in range(3):
        services._spare_verdict(spare, True)
    assert len(calls) == 3


def test_the_slot_probes_dispatch_the_spare_to_its_own_verdict(two, monkeypatch):
    """Запасной слот не идёт ни в зонд активного по хендшейку, ни в резервный
    по хендшейку — у veth хендшейка нет."""
    _guest(two, monkeypatch)
    seen = []
    monkeypatch.setattr(two, "_spare_verdict", lambda g, active: seen.append((g.id, active)) or "ok")
    out = two._probe_slots(two.rt_slots(), two.active_gateway())
    assert seen == [(SPARE, False)] and out[SPARE] == "ok"


# ── сторона резерва: что разрешаю гостю ─────────────────────────────────────

def test_the_reserve_allows_rf_via_only_with_its_switch_its_slot_and_a_live_link(two):
    s = two
    _settle(s)
    v = s.reserve_rf_via_view()
    assert v == {"rf_via": True, "rf_cidr": "169.254.77.4/30"}, v
    settings.set_value("app.reserve.rf_via_mine", False)
    assert s.reserve_rf_via_view()["rf_via"] is False, "админ резерва выключил — разрешение осталось"
    settings.set_value("app.reserve.rf_via_mine", True)
    _both_down(s)
    assert s.reserve_rf_via_view()["rf_via"] is False, "активный слот резерва недоступен — давать нечего"


def test_the_reserve_without_slots_or_with_routing_off_allows_nothing(services, fake_routing):
    assert services.reserve_rf_via_view()["rf_via"] is False
    fake_routing.enabled = False
    assert services.reserve_rf_via_view()["rf_via"] is False


def test_the_state_for_the_guest_carries_rf_via_and_the_guest_keeps_it(two, monkeypatch):
    from tests.integration import _reserve_common as rc
    _settle(two)
    rc.pair_row(two)
    body = two.reserve_guest_state_view()
    assert body["rf_via"] is True and body["rf_cidr"] == "169.254.77.4/30"
    settings.set_value("app.reserve.zone_rf_cidr", "169.254.77.8/30")
    assert two.reserve_guest_state_view()["rf_cidr"] == "169.254.77.8/30"
    two.guest_state_in({**body, "name": "Gamma"})
    st = two.guest_state()
    assert st["rf_via"] is True and st["rf_cidr"] == "169.254.77.4/30"
