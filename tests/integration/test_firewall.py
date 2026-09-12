"""Файервол в потоке бота: реассерт set устройств админа в точках изменения и
разовое снятие старых ворот только при включённой таблице."""
from awgbot.core import config
from awgbot.infra import awg, nftguard


def test_admin_device_creation_reconciles_the_admin_set(services, fake_awg, make_active_client):
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID)
    dc = services.add_device(admin.id, "phone")
    assert fake_awg.fw_admin_ips == [dc.address]


def test_nonadmin_device_creation_does_not_touch_the_firewall(services, fake_awg, make_active_client):
    client = make_active_client(name="Клиент", tg_id=777)
    services.add_device(client.id, "phone")
    assert fake_awg.fw_admin_ips is None


def test_reconcile_collects_only_admin_addresses(services, fake_awg, make_active_client):
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID)
    client = make_active_client(name="Клиент", tg_id=777)
    a1 = services.add_device(admin.id, "a1")
    services.add_device(client.id, "c1")
    a2 = services.add_device(admin.id, "a2")
    fake_awg.fw_admin_ips = None
    services.reconcile_ssh_access()
    assert fake_awg.fw_admin_ips == sorted([a1.address, a2.address])


def test_reconcile_skips_when_firewall_disabled(services, fake_awg, monkeypatch):
    monkeypatch.setattr(nftguard, "enabled", lambda: False)
    services.reconcile_ssh_access()
    assert fake_awg.fw_admin_ips is None and fake_awg.fw_calls == 0


def test_legacy_gate_retired_once_and_only_when_enabled(services, fake_awg, monkeypatch):
    calls = []
    monkeypatch.setattr(awg, "remove_legacy_ssh_gate", lambda: calls.append(1) or True)
    monkeypatch.setattr(nftguard, "enabled", lambda: False)
    services.retire_legacy_ssh_gate()
    assert calls == [], "без новой таблицы старые ворота не трогаем"
    monkeypatch.setattr(nftguard, "enabled", lambda: True)
    services.retire_legacy_ssh_gate()
    services.retire_legacy_ssh_gate()
    assert calls == [1], "снимаются один раз, метка в state"
    assert services.db.get_state("legacy_ssh_gate_removed") == "1"
