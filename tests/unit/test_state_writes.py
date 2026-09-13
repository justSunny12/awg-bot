"""Аудит, п.3: состояние не пишется, если не изменилось; стрики с потолком;
один коммит на тик. Меряем коммитами: спокойный тик обязан стоить ноль."""
from __future__ import annotations

import types

from awgbot.core import settings


def test_set_state_skips_unchanged_value(services):
    db = services.db
    n0 = db.commits
    assert db.set_state("probe_k", "1") is True
    assert db.commits == n0 + 1
    assert db.set_state("probe_k", "1") is False, "то же значение — не пишем"
    assert db.commits == n0 + 1
    assert db.set_state("probe_k", "2") is True
    assert db.get_state("probe_k") == "2"


def test_set_state_hot_key_stays_correct(services):
    db = services.db
    key = next(iter(db._HOT_STATE_KEYS))
    db.set_state(key, "a"); assert db.get_state(key) == "a"
    db.set_state(key, "a"); assert db.get_state(key) == "a"
    db.set_state(key, "b"); assert db.get_state(key) == "b"


def test_set_states_is_one_commit_and_skips_unchanged(services):
    db = services.db
    db.set_state("a", "1")
    n0 = db.commits
    written = db.set_states({"a": "1", "b": "2", "c": "3"})
    assert written == 2 and db.commits == n0 + 1
    assert db.set_states({"a": "1", "b": "2", "c": "3"}) == 0


def test_resource_alerts_quiet_state_costs_no_commits(services, fake_awg):
    streak = settings.get_int("app.monitoring.alert_streak", 5)
    lo = {"cpu": 10, "ram": 10, "disk": 10}
    for _ in range(streak):
        services.check_resource_alerts(dict(lo))
    n0 = services.db.commits
    for _ in range(5):
        assert services.check_resource_alerts(dict(lo)) == []
    assert services.db.commits == n0, "норма держится — ни одной записи"
    assert services.db.get_state("res_lo_cpu") == str(streak), "счётчик упёрся в потолок"


def test_resource_alert_streak_semantics_survive_the_cap(services, fake_awg):
    """Потолок не меняет поведения: алерт на порог, отбой на порог, не раньше."""
    streak = settings.get_int("app.monitoring.alert_streak", 5)
    hi = {"cpu": 95, "ram": 10, "disk": 10}
    lo = {"cpu": 10, "ram": 10, "disk": 10}
    for _ in range(streak * 3):                          # долго высоко: счётчик не «переполняется»
        services.check_resource_alerts(dict(hi))
    assert services.db.get_state("res_alert_cpu") == "1"
    rec = [services.check_resource_alerts(dict(lo)) for _ in range(streak)]
    assert all(r == [] for r in rec[:-1]) and len(rec[-1]) == 1


def test_liveness_tick_quiet_state_costs_no_commits(services, fake_routing):
    fake_routing.probe = "ok"
    for _ in range(services._RT_UP_STREAK + 1):
        services.routing_liveness_tick()
    assert fake_routing.marking is True
    n0 = services.db.commits
    for _ in range(5):
        services.routing_liveness_tick()
    assert services.db.commits == n0, "шлюз жив, состояние не меняется — тик без записей"


def test_liveness_down_counter_is_capped_but_announces(services, fake_routing):
    fake_routing.probe = "ok"
    for _ in range(services._RT_UP_STREAK):
        services.routing_liveness_tick()
    fake_routing.probe = "down"
    notes = []
    for _ in range(services._RT_ANNOUNCE_AFTER * 3):
        notes += services.routing_liveness_tick()
    assert len(notes) == 1, "письмо один раз, независимо от длины провала"
    cap = max(services._RT_DOWN_STREAK, services._RT_ANNOUNCE_AFTER)
    assert int(services.db.get_state(services._RT_DOWN_KEY)) == cap
    n0 = services.db.commits
    services.routing_liveness_tick()
    assert services.db.commits == n0, "затяжной провал — тоже без записей"


# ── агент шлюза ──────────────────────────────────────────────────────────────

def test_gateway_streak_alert_quiet_costs_no_commits(tmp_path):
    from awgbot.domain.gateway import GatewayServices
    from awgbot.infra.db import Database
    db = Database(tmp_path / "gw.db"); db.init_schema()
    svc = GatewayServices(db)
    for _ in range(3):
        svc._streak_alert("t", False, 3, "ПЛОХО", "ОК")
    n0 = db.commits
    for _ in range(4):
        assert svc._streak_alert("t", False, 3, "ПЛОХО", "ОК") == []
    assert db.commits == n0
    # семантика на месте: три плохих → алерт, ещё три плохих — тишина, три хороших → отбой
    fire = lambda bad: svc._streak_alert("t", bad, 3, "ПЛОХО", "ОК")
    assert fire(True) == [] and fire(True) == [] and len(fire(True)) == 1
    assert fire(True) == [] and fire(True) == [] and fire(True) == []
    assert fire(False) == [] and fire(False) == [] and len(fire(False)) == 1


def test_gateway_monitor_tick_is_one_commit_when_quiet(tmp_path, monkeypatch):
    """Спокойный тик: снимок для панели — единственная запись (он меняется
    каждый тик по времени), все стрики — без записи."""
    from awgbot.domain.gateway import GatewayServices, GwStatus, GwCheck
    from awgbot.infra.db import Database
    db = Database(tmp_path / "gw.db"); db.init_schema()
    svc = GatewayServices(db)
    st = GwStatus(link_up=True, handshake_age=10.0, checks=[GwCheck("x", True)], temp=50.0,
                  disk=30.0, throttled={"raw": 0, "now": [], "ever": []},
                  module_version="v", srcversion="s", kernels_total=3)
    monkeypatch.setattr(svc, "status", lambda: st)
    monkeypatch.setattr(svc, "tg_mark_ensure", lambda missing=None: 0)
    for _ in range(6):
        svc.monitor_tick()
    n0 = db.commits
    svc.monitor_tick()
    assert db.commits - n0 <= 1, "снимок и только снимок"
