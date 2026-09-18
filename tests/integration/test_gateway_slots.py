"""Слоты шлюзов (docs/gateway-failover.md): таблица вместо флага, миграция,
автомат переключения по слотам, липкость, интервал, холодный старт, пинг."""
from __future__ import annotations

import sqlite3

import pytest

from awgbot.core import config, settings
from awgbot.domain.services import ServiceError
from awgbot.infra import routing
from awgbot.util import timeutil

ADMIN = config.ADMIN_ID


@pytest.fixture()
def two(services, fake_awg, fake_routing, make_active_client, monkeypatch):
    """Админ с двумя машинами в двух слотах; зонды по слоту управляются
    словарём probe[slot] → вердикт."""
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi")
    pi2 = services.add_device(admin.id, "Pi2")
    g1 = services.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    g2 = services.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    probe = {1: "ok", 2: "ok"}
    monkeypatch.setattr(services, "_probe_slot", lambda g, active=False: probe[g.id])
    monkeypatch.setattr(services, "_rt_standby_interval", lambda: 0)   # зонд резерва каждый такт
    monkeypatch.setattr(services, "_rt_window_size", lambda: 10)        # боевое окно: 10 замеров, порог 5
    switched = []
    monkeypatch.setattr(routing, "switch_active", lambda iface: switched.append(iface))
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: None)
    services.probe, services.switched = probe, switched
    return admin, g1, g2


def _settle(services):
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()


# ── БД ───────────────────────────────────────────────────────────────────────

def test_slot_table_replaces_the_flag(services, make_active_client):
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    a = services.add_device(admin.id, "a"); b = services.add_device(admin.id, "b")
    g = services.db.gateway_add(a.device_id, "awglink", 443, "10.99.99.0/30")
    assert g.id == 1 and g.preferred == 1, "первый слот — предпочтительный сам"
    assert services.db.get_device(a.device_id).is_gateway == 1
    assert services.db.gateway_by_device(a.device_id).id == 1
    g2 = services.db.gateway_add(b.device_id, "awglink2", 8443, "10.99.99.4/30")
    assert g2.id == 2 and g2.preferred == 0
    services.db.gateway_update(2, home_subnets=["192.168.2.0/24"], label="дом 2")
    assert services.db.gateway(2).home_subnets == ["192.168.2.0/24"]
    assert services.db.gateway(2).label == "дом 2"
    services.db.gateway_set_preferred(2)
    assert [x.id for x in services.db.gateways()] == [2, 1]
    with pytest.raises(sqlite3.IntegrityError):
        services.db.gateway_add(a.device_id, "awglink3", 9443, "10.99.99.8/30")
    services.db.gateway_delete(1)
    assert services.db.get_device(a.device_id).is_gateway == 0
    with pytest.raises(ValueError):
        services.db.gateway_update(2, link_if="x")


def test_legacy_flag_migrates_into_slot_one(tmp_path, monkeypatch):
    """БД v2.23: флаг is_gateway и ключи бандла без суффикса → слот 1,
    предпочтительный, активный; ключи с суффиксом; повтор ничего не делает."""
    from awgbot.infra.db import Database
    monkeypatch.setattr(config, "ROUTING_HOME_SUBNETS", ["192.168.1.0/24"])
    db = Database(str(tmp_path / "old.db")); db.init_schema()
    cid = db.create_client(name="Админ", device_limit=0, period_start="2026-01-01",
                           period_end="2027-01-01", invite_code="A")
    a = db.create_device(cid, "NASPi", "PA", "S", "10.8.1.2", private_key="k")
    with db._tx() as cur:
        cur.execute("UPDATE devices SET is_gateway = 1 WHERE id = ?", (a,))
    db.set_state("gw_bundle_issued_at", "2026-09-01T00:00:00+03:00")
    db.set_state("gw_bundle_ssh_allow", "10.8.1.2")
    db.close()
    db = Database(str(tmp_path / "old.db")); db.init_schema()
    gws = db.gateways()
    assert len(gws) == 1 and gws[0].id == 1 and gws[0].device_id == a and gws[0].preferred == 1
    assert gws[0].home_subnets == ["192.168.1.0/24"]
    assert db.get_state("routing_active_gateway") == "1"
    assert db.get_state("gw_bundle_issued_at_1") == "2026-09-01T00:00:00+03:00"
    assert db.get_state("gw_bundle_ssh_allow_1") == "10.8.1.2"
    assert db.get_state("gw_bundle_issued_at") is None
    assert db.get_device(a).is_gateway == 1
    row = db._connection().execute("SELECT is_gateway FROM devices WHERE id = ?", (a,)).fetchone()
    assert row["is_gateway"] == 0, "колонка после переноса пуста"
    db.close()
    db = Database(str(tmp_path / "old.db")); db.init_schema()
    assert len(db.gateways()) == 1
    db.close()


# ── назначение второго слота ─────────────────────────────────────────────────

def test_second_slot_brings_its_own_link_and_needs_no_rekey_of_the_first(
        services, make_active_client, monkeypatch, fake_routing):
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi"); pi2 = services.add_device(admin.id, "Pi2")
    runs = []
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: runs.append((mode, dict(env or {}))))
    services.gateway_setup(pi.device_id)
    assert runs == [("--rekey", {"LINK_IF": "awglink", "LINK_PORT": "443", "LINK_CIDR": "10.99.99.0/30"})], \
        "назначение всегда полным путём: ключи линка новые и у первого слота"
    res = services.gateway_setup(pi2.device_id)
    assert res["gateway"].id == 2 and res["rekeyed"] and res["previous"] is None
    assert runs[-1][0] == "--apply" and runs[-1][1]["LINK_IF"] == "awglink2" \
        and runs[-1][1]["LINK_PORT"] == "8443" and runs[-1][1]["LINK_CIDR"] == "10.99.99.4/30"
    assert services.bundle_name(res["gateway"]) == "awg-gw-bundle-awglink2.sh"
    assert services.gateway_candidates() == []
    with pytest.raises(ServiceError):
        services.gateway_setup(None)                    # третьего слота нет
    assert services.active_gateway().id == 1, "активный не сменился"
    # замена машины во втором слоте — rekey своего линка
    pi3 = services.add_device(admin.id, "Pi3")
    res = services.gateway_setup(pi3.device_id, rekey=True, slot_id=2)
    assert res["previous"].id == pi2.device_id and runs[-1] == ("--rekey", {"LINK_IF": "awglink2", "LINK_PORT": "8443", "LINK_CIDR": "10.99.99.4/30"})
    # снять второй — rollback своего линка, первый жив
    prev = services.gateway_remove(2)
    assert prev.id == pi3.device_id and runs[-1][0] == "--rollback" and runs[-1][1]["LINK_IF"] == "awglink2"
    assert [g.id for g in services.db.gateways()] == [1]


def test_removing_the_active_slot_hands_traffic_to_the_other(two, services, monkeypatch):
    admin, g1, g2 = two
    store = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: store.__setitem__(k, v) or [k])
    services.gateway_remove(1)
    assert services.active_gateway().id == 2 and services.switched == ["awglink2"]
    assert "app.routing.enabled" not in store, "резерв есть — функцию не выключаем"
    services.gateway_remove(2)
    assert store.get("app.routing.enabled") is False and services.active_gateway() is None


# ── автомат ──────────────────────────────────────────────────────────────────

def test_failover_switches_after_threshold_and_stays(two, services):
    """Активный лежит порог тактов, резерв в порядке → переключение без
    снятия маркировки; оживший прежний остаётся в резерве (липкость)."""
    admin, g1, g2 = two
    _settle(services)
    assert services.active_gateway().id == 1 and services.db.get_state(services._RT_LINK_KEY) == "1"
    services.probe[1] = "down"
    notes = []
    for _ in range(services._rt_fail_need()):
        notes += services.routing_liveness_tick()
    assert services.active_gateway().id == 2 and services.switched == ["awglink2"]
    assert services.db.get_state(services._RT_LINK_KEY) == "1", "маркировка не снималась ни на такт"
    assert len(notes) == 1 and "переключён" in notes[0].text and "Pi2" in notes[0].text
    # прежний ожил — остаётся в резерве, одно письмо
    services.probe[1] = "ok"
    notes = []
    for _ in range(services._RT_UP_STREAK):
        notes += services.routing_liveness_tick()
    assert services.active_gateway().id == 2, "липкость: обратно не возвращаемся"
    assert len(notes) == 1 and "остаётся в резерве" in notes[0].text
    assert services.routing_liveness_tick() == []


def test_failover_needs_a_healthy_candidate_else_degrades(two, services):
    admin, g1, g2 = two
    _settle(services)
    services.probe[1] = services.probe[2] = "down"
    notes = []
    for _ in range(services._rt_fail_need()):
        notes += services.routing_liveness_tick()
    assert services.active_gateway().id == 1 and services.switched == []
    assert services.db.get_state(services._RT_LINK_KEY) == "0", "гашение, как без резерва"
    assert len(notes) == 1 and "тоже не отвечает" in notes[0].text
    # резерв ожил — вытаскивает из деградации
    services.probe[2] = "ok"
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    assert services.active_gateway().id == 2 and services.db.get_state(services._RT_LINK_KEY) == "1"


def test_second_switch_within_the_interval_is_refused(two, services):
    admin, g1, g2 = two
    _settle(services)
    services.probe[1] = "down"
    for _ in range(services._rt_fail_need()):
        services.routing_liveness_tick()
    assert services.active_gateway().id == 2
    services.probe[1] = "ok"
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    services.probe[2] = "down"
    notes = []
    for _ in range(services._rt_fail_need()):
        notes += services.routing_liveness_tick()
    assert services.active_gateway().id == 2, "второе переключение подряд не делаем"
    assert services.db.get_state(services._RT_LINK_KEY) == "0"
    assert len(notes) == 1 and "Второе переключение" in notes[0].text
    # интервал прошёл — можно
    old = timeutil.to_iso(timeutil.now() - __import__("datetime").timedelta(minutes=30))
    services.db.set_state(services._RT_SWITCHED_KEY, old)
    services.routing_liveness_tick()
    assert services.active_gateway().id == 1


def test_failover_can_be_switched_off(two, services, monkeypatch):
    admin, g1, g2 = two
    real = settings.get_bool
    monkeypatch.setattr(settings, "get_bool", lambda k, d=False: False if k == "app.routing.failover.enabled" else real(k, d))
    _settle(services)
    services.probe[1] = "down"
    for _ in range(services._rt_fail_need()):
        services.routing_liveness_tick()
    assert services.active_gateway().id == 1 and services.db.get_state(services._RT_LINK_KEY) == "0"


def test_standby_outage_is_announced_late_and_quietly(two, services):
    admin, g1, g2 = two
    _settle(services)
    services.probe[2] = "down"
    notes = []
    for _ in range(services._rt_window_size() * 2):
        notes += services.routing_liveness_tick()
    assert services.active_gateway().id == 1 and services.db.get_state(services._RT_LINK_KEY) == "1"
    assert len(notes) == 1 and "Резервный" in notes[0].text and not notes[0].critical
    assert services.routing_liveness_tick() == []
    services.probe[2] = "ok"
    notes = []
    for _ in range(services._RT_UP_STREAK):
        notes += services.routing_liveness_tick()
    assert len(notes) == 1 and "снова отвечает" in notes[0].text


def test_manual_switch_ignores_thresholds_and_the_interval(two, services):
    admin, g1, g2 = two
    _settle(services)
    services.gateway_switch(2, manual=True)
    assert services.active_gateway().id == 2 and services.switched == ["awglink2"]
    assert services.db.get_state(services._RT_SWITCHED_KEY) is None, "ручное — не в интервал автомата"
    services.gateway_switch(1, manual=True)
    assert services.active_gateway().id == 1
    assert services.gateway_switch(1, manual=True).id == 1 and services.switched == ["awglink2", "awglink"]


def test_ping_cache_is_invalidated_when_the_host_falls(two, services, monkeypatch):
    admin, g1, g2 = two
    monkeypatch.setattr(routing, "ping_peer", lambda iface="", **k: 43)
    monkeypatch.setattr(routing, "link_peer_endpoint", lambda iface="": "198.51.100.7" if iface == "awglink2" else None)
    assert services.gateway_ping(2) == 43 and services.gateway_ping_cached(2)[0] == 43
    assert services.gateway_ping_lazy(2) == 43
    assert services.gateway_external_ip(2) == "198.51.100.7" and services.gateway_external_ip(1) is None
    _settle(services)
    services.probe[2] = "down"
    for _ in range(services._rt_fail_need()):
        services.routing_liveness_tick()
    assert services.gateway_ping_cached(2) is None, "хост упал — пинг устарел"
    monkeypatch.setattr(routing, "ping_peer", lambda iface="", **k: None)
    assert services.gateway_ping(2) is None
    monkeypatch.setattr(routing, "ping_peer", lambda iface="", **k: 1200)
    assert services.gateway_ping_lazy(2) == 1200
    from awgbot.bot import texts
    assert texts.ping_fmt(43) == "43 мс" and texts.ping_fmt(1200) == "1,2 с"


# ── холодный старт ───────────────────────────────────────────────────────────

def test_cold_start_prefers_the_preferred_slot_only_when_the_host_rebooted(two, services, monkeypatch):
    """Холодный старт — по моменту загрузки хоста: первый такт живости стартует
    раньше этой проверки, и по последнему такту его не отличить."""
    from awgbot.runtime import hostmetrics
    admin, g1, g2 = two
    services.db.set_state(services._RT_ACTIVE_KEY, "2")
    now = int(timeutil.now().timestamp())
    services.db.set_state(services._RT_BOOT_KEY, str(now - 100000))
    services.routing_liveness_tick()                       # такт уже был — не помеха
    # тёплый: тот же момент загрузки — сохранённый активный
    monkeypatch.setattr(hostmetrics, "read_uptime_seconds", lambda: 100000)
    assert services.routing_cold_start().id == 2 and services.switched == []
    # холодный: хост поднялся минуту назад — предпочтительный (слот 1), он отвечает
    monkeypatch.setattr(hostmetrics, "read_uptime_seconds", lambda: 60)
    assert services.routing_cold_start().id == 1 and services.switched == ["awglink"]
    assert services.db.get_state(services._RT_BOOT_KEY) == str(int(timeutil.now().timestamp()) - 60)
    # тот же момент загрузки второй раз — уже тёплый
    services.db.set_state(services._RT_ACTIVE_KEY, "2")
    assert services.routing_cold_start().id == 2

    def cold():                                            # «хост перезагрузился» заново
        services.db.set_state(services._RT_BOOT_KEY, str(now - 100000))
    # холодный, предпочтительный молчит → сохранённый (2) отвечает
    cold(); services.db.set_state(services._RT_ACTIVE_KEY, "2")
    services.probe[1] = "down"
    assert services.routing_cold_start().id == 2
    # все молчат → предпочтительный всё равно
    cold(); services.probe[2] = "down"
    assert services.routing_cold_start().id == 1
    # предпочтительного нет — сохранённый
    cold(); services.db.gateway_set_preferred(None)
    services.db.set_state(services._RT_ACTIVE_KEY, "2")
    assert services.routing_cold_start().id == 2


def test_startup_warnings_name_the_slot(two, services):
    admin, g1, g2 = two
    assert services.routing_startup_warnings() == []
    services.probe[2] = "down"
    warns = services.routing_startup_warnings()
    assert len(warns) == 1 and warns[0].startswith("Резервный шлюз") and "Pi2" in warns[0]
    services.probe[1] = "down"
    warns = services.routing_startup_warnings()
    assert len(warns) == 1 and "«NASPi» и «Pi2» не отвечают" in warns[0], "оба лежат — одной строкой"
    services.probe[2] = "ok"
    warns = services.routing_startup_warnings()
    assert len(warns) == 1 and warns[0].startswith("Шлюз условной маршрутизации") and "NASPi" in warns[0]


def test_home_subnets_are_parsed_and_conflicts_reported(two, services):
    admin, g1, g2 = two
    res = services.gateway_set_home_subnets(1, "192.168.1.0/24, мусор 10.8.1.0/24 10.99.99.0/30 2001:db8::/64")
    assert res["kept"] == ["192.168.1.0/24"] and res["conflict"] is None
    reasons = dict(res["rejected"])
    assert "мусор" in reasons and "10.8.1.0/24" in reasons and "10.99.99.0/30" in reasons
    res = services.gateway_set_home_subnets(2, "192.168.1.1/24")
    assert res["kept"] == ["192.168.1.0/24"] and res["conflict"].id == 1
    assert services.gateway_slots_policy() == [(1, "awglink", ["192.168.1.0/24"]), (2, "awglink2", [])]
    assert services.gateway_set_home_subnets(2, "—")["kept"] == []
    services.gateway_set_label(2, "  дом   2  очень длинная подпись сверх меры")
    assert services.db.gateway(2).label == "дом 2 очень длинная"


def test_standby_is_probed_rarely_and_lives_by_handshake_in_between(two, services, monkeypatch):
    """Маячок наружу с домашнего адреса каждые полминуты — сигнатура: резерв
    зондируется редко, с джиттером; между зондами живость — по хендшейку."""
    admin, g1, g2 = two
    monkeypatch.setattr(services, "_rt_standby_interval", lambda: 5)
    probes = []
    monkeypatch.setattr(services, "_probe_slot", lambda g, active=False: probes.append(g.id) or "ok")
    ages = {"awglink2": 30}
    monkeypatch.setattr(routing, "link_handshake_age", lambda iface="": ages.get(iface))
    for _ in range(services._RT_UP_STREAK + 2):
        services.routing_liveness_tick()
    assert probes.count(2) == 1, "резерв зондирован один раз, дальше — по хендшейку"
    assert probes.count(1) == services._RT_UP_STREAK + 2, "активный — каждый такт"
    st = next(x for x in services.gateway_states() if x["gateway"].id == 2)
    assert st["link_ok"], "хендшейк свежий, последний зонд прошёл — резерв в порядке"
    ages["awglink2"] = 600                                  # хендшейк протух
    for _ in range(services._rt_fail_need()):
        services.routing_liveness_tick()
    st = next(x for x in services.gateway_states() if x["gateway"].id == 2)
    assert not st["link_ok"] and probes.count(2) == 1, "без хендшейка резерв мёртв без единого зонда"


def test_manual_switch_to_a_dead_gateway_holds_the_automaton(two, services):
    """Админ переложил трафик на лежащий шлюз — значит, так надо: автомат не
    возвращает. Ожил и упал снова — автомат переключает, как обычно."""
    admin, g1, g2 = two
    _settle(services)
    services.probe[2] = "down"
    for _ in range(services._rt_fail_need()):
        services.routing_liveness_tick()
    services.gateway_switch(2, manual=True)
    assert services.db.get_state(services._RT_HOLD_KEY) == "2"
    for _ in range(services._rt_fail_need() + 2):
        services.routing_liveness_tick()
    assert services.active_gateway().id == 2, "удержание: автомат не перекладывает обратно"
    assert services.db.get_state(services._RT_LINK_KEY) == "0"
    services.probe[2] = "ok"
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    assert services.db.get_state(services._RT_HOLD_KEY) == "", "ожил — удержание снято"
    services.probe[2] = "down"
    for _ in range(services._rt_fail_need()):
        services.routing_liveness_tick()
    assert services.active_gateway().id == 1, "упал снова — автомат переключил на живой"
    # ручное на ЖИВОЙ (три хороших) — удержания нет
    services.probe[2] = "ok"
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    services.gateway_switch(2, manual=True)
    assert services.db.get_state(services._RT_HOLD_KEY) == ""


def test_fresh_standby_shows_link_check_until_three_good(two, services):
    from awgbot.bot import texts
    admin, g1, g2 = two
    st = next(x for x in services.gateway_states() if x["gateway"].id == 2)
    assert texts.slot_status(st) == "⏳ <b>[Резерв]</b>, проверка связи…"
    _settle(services)
    st = next(x for x in services.gateway_states() if x["gateway"].id == 2)
    assert texts.slot_status(st) == "🟢 <b>[Резерв]</b>"
    services.probe[2] = "down"
    for _ in range(services._rt_fail_need()):
        services.routing_liveness_tick()
    st = next(x for x in services.gateway_states() if x["gateway"].id == 2)
    assert texts.slot_status(st).startswith("🔴 <b>[Резерв]</b>, ") and "мин" in texts.slot_status(st)


def test_first_tick_lays_slot_policy_before_probing(two, services, monkeypatch):
    """Без правила по метке зонд резерва ушёл бы через основную таблицу ВПС и
    «прошёл» при мёртвом линке: первый такт ставит обвязку слотов до зондов."""
    admin, g1, g2 = two
    order = []
    monkeypatch.setattr(services, "_ensure_gateway_policy", lambda: order.append("policy"))
    monkeypatch.setattr(services, "_probe_slot", lambda g, active=False: order.append(f"probe{g.id}") or "ok")
    services.routing_liveness_tick()
    assert order[0] == "policy" and {"probe1", "probe2"} <= set(order[1:])


def test_standby_needs_a_fresh_handshake_even_when_its_probe_passes(two, services, monkeypatch):
    """Зонд прошёл, а хендшейка нет — путь ушёл мимо линка: такой резерв
    нагрузку не примет и кандидатом не считается."""
    admin, g1, g2 = two
    monkeypatch.setattr(services, "_rt_standby_interval", lambda: 5)
    monkeypatch.setattr(routing, "link_handshake_age", lambda iface="": None)
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    st = next(x for x in services.gateway_states() if x["gateway"].id == 2)
    assert not st["link_ok"]
    services.probe[1] = "down"
    for _ in range(services._rt_fail_need()):
        services.routing_liveness_tick()
    assert services.active_gateway().id == 1, "на резерв без хендшейка не переключаемся"


def test_link_changes_refresh_the_host_firewall_and_clear_the_hold(services, make_active_client, monkeypatch, fake_routing):
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = services.add_device(admin.id, "NASPi"); pi2 = services.add_device(admin.id, "Pi2")
    monkeypatch.setattr(services, "_run_link_script", lambda mode, env=None: None)
    monkeypatch.setattr(routing, "switch_active", lambda iface: None)
    refreshed = []
    monkeypatch.setattr(services, "_gw_firewall_refresh", lambda: refreshed.append(1))
    services.gateway_setup(pi.device_id)
    assert refreshed == [], "первый слот — линк обвязки, порт уже открыт"
    services.gateway_setup(pi2.device_id)
    assert refreshed == [1], "второй линк — порт в файервол сразу"
    services.db.set_state(services._RT_HOLD_KEY, "2")
    services.gateway_remove(2)
    assert refreshed == [1, 1] and services.db.get_state(services._RT_HOLD_KEY) == ""


def test_switch_on_the_third_failure_in_a_sliding_window(two, services, monkeypatch):
    """Боевое окно: 10 замеров (5 мин), порог 50 % → пятый неуспешный в окне,
    подряд или вразнобой, переключает сразу; замеры старше окна не считаются."""
    admin, g1, g2 = two
    monkeypatch.setattr(services, "_rt_window_size", lambda: 10)
    _settle(services)
    services.probe[1] = "down"
    for _ in range(4):
        services.routing_liveness_tick()
    assert services.active_gateway().id == 1, "четыре неудачи — ещё нет"
    assert services.db.get_state(services._RT_LINK_KEY) == "1", "и маркировка на месте: одно окно на всё"
    services.routing_liveness_tick()
    assert services.active_gateway().id == 2, "пятая — переключение сразу"
    assert services.db.get_state(services._RT_LINK_KEY) == "1", "маркировка не снималась ни на такт"
    # обратно, руками; вразнобой: плохой, хороший, … — пятая неудача на девятом замере
    services.probe[1] = "ok"
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    services.gateway_switch(1, manual=True)
    services.db.set_state(services._RT_SWITCHED_KEY, "")
    for v in ["down", "ok", "down", "ok", "down", "ok", "down", "ok"]:
        services.probe[1] = v
        services.routing_liveness_tick()
    assert services.active_gateway().id == 1
    services.probe[1] = "down"
    services.routing_liveness_tick()
    assert services.active_gateway().id == 2, "пятая неудача за пять минут — переключение"
    # окно скользит: четыре неудачи, потом девять хороших, потом одна — в окне только одна
    services.probe[1] = "ok"
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    services.gateway_switch(1, manual=True)
    services.db.set_state(services._RT_SWITCHED_KEY, "")
    for v in ["down"] * 4 + ["ok"] * 9 + ["down"]:
        services.probe[1] = v
        services.routing_liveness_tick()
    assert services.active_gateway().id == 1, "старые неудачи выпали из окна"


def test_switch_starts_the_new_active_with_a_clean_window(two, services, monkeypatch):
    """У резерва в окне могли быть неудачи: после переключения они не должны
    тут же тянуть трафик обратно — окно нового активного начинается заново."""
    admin, g1, g2 = two
    monkeypatch.setattr(services, "_rt_window_size", lambda: 10)
    _settle(services)
    services.probe[2] = "down"
    for _ in range(4):
        services.routing_liveness_tick()                   # четыре неудачи резерва в окне
    services.probe[2] = "ok"
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    services.probe[1] = "down"
    for _ in range(5):
        services.routing_liveness_tick()
    assert services.active_gateway().id == 2
    services.db.set_state(services._RT_SWITCHED_KEY, "")                 # интервал не мешает
    services.probe[1] = "ok"
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    services.probe[2] = "down"
    services.routing_liveness_tick()
    assert services.active_gateway().id == 2, "одна неудача после переключения — не пятая"
