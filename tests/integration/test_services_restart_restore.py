"""Integration: реставрация app-устройства, детект рестарта контейнера,
переналожение блокировок, гистерезис алертов загрузки, чистка истории.
"""
import json

import pytest

from awgbot.core import config
from awgbot.core import settings
from awgbot.core.blocks import DeviceBlock
from awgbot.domain import configgen
from awgbot.domain.services import ServiceError
from awgbot.infra import awg

pytestmark = pytest.mark.integration


def _vpn_link(priv, pub, ip):
    """Минимальный валидный vpn:// (ровно то, что читает classify_vpn_link)."""
    obj = {"containers": [{"awg": {"last_config": json.dumps(
        {"client_priv_key": priv, "client_pub_key": pub, "client_ip": ip})}}]}
    return configgen.encode_vpn(obj)


# ── restore_app_device ───────────────────────────────────────────────────────
def test_restore_app_device_promotes_to_bot(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=900)
    priv = "RESTPRIV"
    derived = awg.pubkey_of(priv)                           # fake: 'derived-RESTPRIV'
    did = services.db.create_device(client.id, "app-dev", derived, "PSK", "10.8.0.7", private_key=None)
    services.restore_app_device(did, _vpn_link(priv, "ignored", "10.8.0.7"))
    dev = services.db.get_device(did)
    assert dev.is_managed
    assert dev.private_key == priv                          # ключ записан → полный доступ


def test_restore_app_device_wrong_device(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=901)
    did = services.db.create_device(client.id, "app-dev", "SOMEOTHERPUB", "PSK", "10.8.0.8", private_key=None)
    # ссылка валидна, но её priv деривит не в тот pubkey
    with pytest.raises(ServiceError, match="WRONG_DEVICE"):
        services.restore_app_device(did, _vpn_link("OTHERPRIV", "x", "10.8.0.8"))


def test_restore_rejects_already_bot(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=902)
    dc = services.add_device(client.id, "d")                # уже bot-устройство
    with pytest.raises(ServiceError):
        services.restore_app_device(dc.device_id, "vpn://irrelevant")


# ── detect_and_handle_restart + reconcile_blocks ─────────────────────────────
def test_first_start_records_but_no_reconcile(services, fake_awg):
    # первый запуск: stored=None → фиксируем started_at, рестартом не считаем
    assert services.detect_and_handle_restart() is False
    assert services.db.get_state("container_started_at") == fake_awg.started_at


def test_restart_detected_reapplies_blocks(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=903)
    dc = services.add_device(client.id, "d")
    services._device_set_block(dc.device_id, DeviceBlock.EXPIRY)
    dev = services.db.get_device(dc.device_id)
    services.detect_and_handle_restart()                    # зафиксировать текущий started_at
    # контейнер перезапустился → новый StartedAt, эфемерные DROP'ы слетели
    fake_awg.blocked.discard(dev.address)
    fake_awg.started_at = "2026-02-02T00:00:00+03:00"
    assert services.detect_and_handle_restart() is True
    assert dev.address in fake_awg.blocked                  # DROP переналожен


def test_reconcile_blocks_skips_unblocked_devices(services, fake_awg, make_active_client):
    client = make_active_client(tg_id=904)
    dc = services.add_device(client.id, "d")                # block_reason == 0
    fake_awg.blocked.clear()
    services.reconcile_blocks()
    dev = services.db.get_device(dc.device_id)
    assert dev.address not in fake_awg.blocked              # незаблокированных не трогаем


# ── check_resource_alerts (гистерезис по стрикам) ────────────────────────────
def test_resource_alert_fires_after_streak_and_recovers(services, fake_awg):
    streak = settings.get_int("app.monitoring.alert_streak", 5)
    hi = {"cpu": 95, "ram": 10, "disk": 10}
    fired = []
    for _ in range(streak):
        fired.append(services.check_resource_alerts(dict(hi)))
    # алерт ровно на достижении стрика, ни раньше, ни дважды
    assert all(n == [] for n in fired[:-1])
    assert len(fired[-1]) == 1 and "CPU" in fired[-1][0].text
    # держится выше — повторно не спамит
    assert services.check_resource_alerts(dict(hi)) == []
    # вернулось в норму на streak замеров подряд → один «отбой»
    lo = {"cpu": 10, "ram": 10, "disk": 10}
    rec = [services.check_resource_alerts(dict(lo)) for _ in range(streak)]
    assert all(n == [] for n in rec[:-1])
    assert len(rec[-1]) == 1 and "норм" in rec[-1][0].text.lower()


def test_resource_alert_none_metric_does_not_move_counters(services, fake_awg):
    streak = settings.get_int("app.monitoring.alert_streak", 5)
    # None по CPU не должен ни копить превышение, ни давать ложный отбой
    for _ in range(streak + 2):
        assert services.check_resource_alerts({"cpu": None, "ram": 10, "disk": 10}) == []
    assert services.db.get_state("res_alert_cpu") in (None, "0")


# ── purge_old_history ────────────────────────────────────────────────────────
def test_purge_old_history_returns_counts(services, fake_awg):
    # на пустой БД удалять нечего — но SQL по всем _histories должен отработать
    removed = services.purge_old_history()
    assert isinstance(removed, dict)
    assert all(isinstance(v, int) for v in removed.values())


def test_refresh_status_now_writes_state(services, monkeypatch):
    """Кнопка «Обновить»: живой снимок статуса+метрик пишется в state,
    чтобы последующий server_status_cached отдал свежее без docker exec."""
    from awgbot.infra import awg
    monkeypatch.setattr(awg, "awg_responding", lambda: True)
    monkeypatch.setattr(awg, "container_started_at", lambda: "2026-01-01T00:00:00Z")
    services.db.set_state("last_server_ok", "0")          # было «лежит»
    services.refresh_status_now()
    assert services.db.get_state("last_server_ok") == "1"  # обновилось на «жив»
    from awgbot.runtime import hostmetrics
    snap = hostmetrics.get_host_metrics(services.db)
    assert snap is not None and "cpu" in snap             # метрики записаны
