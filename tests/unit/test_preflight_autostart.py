"""Preflight: автозагрузка awg-интерфейса в host-режиме и мусорные юниты."""
from __future__ import annotations

import awgbot.core.config as config
from awgbot.runtime import preflight


def _fake_units(states):
    states = {"awg-bot": "enabled", **states}
    return lambda unit: states.get(unit, "not-found")


def test_disabled_awg_quick_unit_is_reported(monkeypatch):
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg1")
    monkeypatch.setattr(preflight, "_unit_enabled", _fake_units({"awg-quick@awg1": "disabled"}))
    warns = preflight._host_autostart_warnings()
    assert len(warns) == 1 and "systemctl enable awg-quick@awg1" in warns[0]


def test_enabled_unit_and_no_stale_units_is_quiet(monkeypatch):
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg1")
    monkeypatch.setattr(preflight, "_unit_enabled", _fake_units({"awg-quick@awg1": "enabled"}))
    assert preflight._host_autostart_warnings() == []


def test_stale_lists_units_are_reported_with_cleanup(monkeypatch):
    monkeypatch.setattr(config, "AWG_RUNTIME", "host")
    monkeypatch.setattr(config, "AWG_INTERFACE", "awg1")
    monkeypatch.setattr(preflight, "_unit_enabled", _fake_units({
        "awg-quick@awg1": "enabled", "awg-bot-lists.timer": "enabled",
        "awg-bot-lists.service": "failed"}))
    warns = preflight._host_autostart_warnings()
    assert len(warns) == 1 and "awg-bot-lists.timer" in warns[0] and "disable --now" in warns[0]


def test_docker_runtime_is_out_of_scope(monkeypatch):
    monkeypatch.setattr(config, "AWG_RUNTIME", "docker")
    monkeypatch.setattr(preflight, "_unit_enabled", _fake_units({"awg-quick@awg0": "disabled"}))
    assert preflight._host_autostart_warnings() == []


def test_disabled_bot_unit_is_reported_for_both_roles(monkeypatch):
    monkeypatch.setattr(preflight, "_unit_enabled",
                        lambda unit: "disabled" if unit == "awg-bot" else "enabled")
    assert any("systemctl enable awg-bot" in w for w in preflight._service_autostart_warning())
    monkeypatch.setattr(config, "AWG_RUNTIME", "docker")
    assert any("awg-bot" in w for w in preflight._host_autostart_warnings())
