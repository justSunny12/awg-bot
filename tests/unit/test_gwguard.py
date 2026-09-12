"""gwguard — сторона шлюза: чтение таблицы awg_gw_guard, локальные добавки,
дрейф списка устройств админа относительно бандла."""
from __future__ import annotations

import json
import subprocess

from awgbot.core import config
from awgbot.infra import gwguard


def _cp(rc=0, out=""):
    return subprocess.CompletedProcess([], rc, stdout=out.encode(), stderr=b"")


def test_table_info_parses_sets_and_chains(monkeypatch):
    doc = {"nftables": [
        {"metainfo": {}},
        {"set": {"name": "tunnel_nets4", "elem": [{"prefix": {"addr": "10.9.1.0", "len": 24}},
                                                   {"prefix": {"addr": "10.99.99.0", "len": 30}}]}},
        {"set": {"name": "ssh_allow4", "elem": ["10.9.1.2", {"elem": {"val": "10.9.1.3"}}]}},
        {"chain": {"name": "input"}}, {"chain": {"name": "forward"}},
    ]}
    monkeypatch.setattr(gwguard, "_nft", lambda a, timeout=10: _cp(0, json.dumps(doc)))
    info = gwguard.table_info()
    assert info["sets"]["tunnel_nets4"] == {"10.9.1.0/24", "10.99.99.0/30"}
    assert info["sets"]["ssh_allow4"] == {"10.9.1.2", "10.9.1.3"}
    assert info["chains"] == {"input", "forward"}


def test_table_info_none_when_absent(monkeypatch):
    monkeypatch.setattr(gwguard, "_nft", lambda a, timeout=10: _cp(1))
    assert gwguard.table_info() is None


def test_extra_roundtrip_and_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(gwguard, "FW_ENV", str(tmp_path / "firewall.env"))
    assert gwguard.read_extra() == []
    gwguard.write_extra(["10.9.1.7", "192.168.68.0/24"])
    assert gwguard.read_extra() == ["10.9.1.7", "192.168.68.0/24"]
    text = (tmp_path / "firewall.env").read_text(encoding="utf-8")
    assert 'SSH_ALLOW_EXTRA="10.9.1.7 192.168.68.0/24"' in text, "формат sh-переменной для юнита"
    import pytest
    with pytest.raises(ValueError):
        gwguard.write_extra(["not-an-ip"])


def test_unit_ssh_allow_reads_the_bundle_value(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GW_UNIT", "awg-link-gw.service")
    unit = tmp_path / "awg-link-gw.service"
    unit.write_text('[Service]\nEnvironment=LINK_IF=awglink\nEnvironment="SSH_ALLOW=10.9.1.2 10.9.1.3"\n',
                    encoding="utf-8")
    import pathlib
    real = pathlib.Path.read_text
    monkeypatch.setattr(pathlib.Path, "read_text",
                        lambda self, *a, **k: real(unit, *a, **k) if str(self).endswith("awg-link-gw.service") else real(self, *a, **k))
    assert gwguard.unit_ssh_allow() == ["10.9.1.2", "10.9.1.3"]


def test_forward_policy_from_foreign_chain(monkeypatch):
    doc = {"nftables": [{"chain": {"name": "FORWARD", "policy": "drop"}}]}
    monkeypatch.setattr(gwguard, "_nft", lambda a, timeout=10: _cp(0, json.dumps(doc)))
    assert gwguard.iptables_forward_policy() == "drop"
    monkeypatch.setattr(gwguard, "_nft", lambda a, timeout=10: _cp(1))
    assert gwguard.iptables_forward_policy() is None


# ── ВПС: напоминание о перевыпуске при смене устройств админа ────────────────

def test_gw_bundle_drift_notifies_once_per_change(services, make_active_client, monkeypatch):
    monkeypatch.setattr(config, "ROUTING_ENABLED", True)
    admin = make_active_client(name="Админ", tg_id=config.ADMIN_ID)
    assert services.gw_bundle_drift_notes() == [], "бандл ещё не собирали — молчим"
    services.db.set_state(services._GW_BUNDLE_SSH_KEY, "")      # бандл собран с пустым списком
    services.add_device(admin.id, "phone")
    notes = services.gw_bundle_drift_notes()
    assert len(notes) == 1 and "Перевыпусти" in notes[0].text
    assert services.gw_bundle_drift_notes() == [], "то же расхождение второй раз не шлём"
    services.add_device(admin.id, "laptop")
    assert len(services.gw_bundle_drift_notes()) == 1, "новое расхождение — новое напоминание"
