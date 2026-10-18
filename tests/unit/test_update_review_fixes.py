"""Мелкие правки по ревью этапов 0–2: тексты хода (logic-11, logic-14),
digest прежних версий через цепочку (guard-5), апдейтер без второго запроса
к GitHub (вне оси), «та же версия» при восстановлении — строго у сборок из
выпуска (отступление 1 этапа 2).
"""
from __future__ import annotations

import hashlib

import pytest

import awgbot.core.config as cfg
from awgbot.bot.texts import fleet as ft
from awgbot.infra import updates
from tests.unit import _fleet_common as _fc
from tests.unit._fleet_common import rel

fleet = _fc.fleet
pytestmark = pytest.mark.unit

BOTH = {"requires": "3.4.0"}


@pytest.mark.parametrize("steps, undo, want", [
    ([["main", "v3.5.0"], ["gw", 1, "v3.5.0"], ["gw", 2, "v3.5.0"]], 3, "вернётся последним"),
    ([["gw", 1, "v3.5.0"], ["main", "v3.5.0"]], 2, "вернётся следующим"),            # needs_gw: шлюзы раньше
    ([["gw", 1, "v3.5.0"], ["main", "v3.5.0"], ["gw", 2, "v3.5.0"]], 3, "вернётся в свою очередь"),
])
def test_the_server_line_of_a_rollback_follows_what_is_left(steps, undo, want):
    run = {"state": "rolling_back", "steps": steps, "undo": undo, "prev": "v3.4.0", "tag": "v3.5.0"}
    plan = {"run": run, "server": "v3.5.0", "gateways": []}
    assert f"🟡 Сервер AWG — v3.5.0, {want}" in ft.fleet_live(plan)


def test_the_confirm_names_the_agents_the_server_needs(fleet):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.4.1", gw=BOTH),
                   rel("v3.5.0", main={"requires": "3.4.0", "needs_gw": "3.4.1"}, gw={"requires": "3.4.0"}))
    fleet.slot(1)
    plan = fleet.s.fleet_plan(fleet.s.fleet_target())
    assert plan["needs_gw"] and plan["needs_gw_ver"] == "v3.4.1"
    assert "нужны агенты шлюзов не ниже v3.4.1" in ft.fleet_confirm(plan)



def test_the_updater_gets_the_verified_sha256_and_the_hold_id(monkeypatch):
    from awgbot.infra import detach
    seen = []
    monkeypatch.setattr(detach, "spawn_detached", lambda argv, unit_prefix, env=None: seen.append(env))
    updates.apply(b"DUMMY", hold="a1b2c3d4", sha256="c" * 64)
    assert seen[0]["AWG_UPDATE_HOLD"] == "a1b2c3d4" and seen[0]["AWG_BOT_VERIFIED_SHA256"] == "c" * 64
    updates.apply(b"DUMMY")
    assert "AWG_UPDATE_HOLD" not in seen[1] and "AWG_BOT_VERIFIED_SHA256" not in seen[1]
    import os
    for env in seen:
        os.remove(env["AWG_UPDATE_CLEANUP"])


def test_the_digest_of_each_version_survives_a_chain_and_its_rollback(services, monkeypatch, tmp_path):
    """v3.4.0 → v3.4.1 → v3.5.0 одним ходом, откат набора — сразу на v3.4.0:
    digest первой версии не потерян второй записью."""
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "bot.db")
    d = {v: f"{v} " + hashlib.sha256(v.encode()).hexdigest() for v in ("3.4.0", "3.4.1", "3.5.0")}
    services.db.set_state("update_installed_digest", d["3.4.0"])
    for v in ("3.4.1", "3.5.0"):
        monkeypatch.setattr(cfg, "INSTALLED_VERSION", v)
        (tmp_path / "installed-digest").write_text(d[v] + "\n", encoding="utf-8")
        assert services.adopt_installed_digest() is True
    monkeypatch.setattr(cfg, "INSTALLED_VERSION", "3.4.0")
    services.adopt_installed_digest()
    assert services.db.get_state("update_installed_digest") == d["3.4.0"], "digest первой версии цепочки потерян"
    kept = services.db.get_state("update_installed_digest_prev").splitlines()
    assert kept == [d["3.5.0"], d["3.4.1"]]


@pytest.mark.parametrize("ver, cur, releases, same", [
    ("3.4.0.1", "3.4.0", True, False),        # хотфикс из выпуска — переход
    ("3.4.0", "3.4.0.1", True, False),
    ("3.4.0.2", "3.4.0", False, True),        # локальная сборка — как раньше, по X.Y.Z
    ("3.4.1", "3.4.0", False, False),
    ("3.4.0", "dev", True, True),             # не разобрана — без перехода
])
def test_the_same_version_is_strict_for_release_builds(ver, cur, releases, same):
    from awgbot.domain.fleet.restore import same_version
    assert same_version(ver, cur, releases=releases) is same


def test_a_hotfix_backup_on_its_base_release_is_a_transition(monkeypatch):
    from awgbot.domain.fleet import restore as fr
    blob = b"DUMMY-hotfix"
    hexd = hashlib.sha256(blob).hexdigest()
    monkeypatch.setattr(updates, "list_releases", lambda **kw: [
        updates.Release(tag="v3.4.0.1", version=(3, 4, 0, 1), body="", asset_url="DUMMY", sha256=hexd,
                        meta={"awg_gen": 1})])
    label = {"version": "3.4.0.1", "digest": f"sha256:{hexd}"}
    assert fr.plan_release(label, installed="3.4.0", installed_release=True)["kind"] == fr.UP
    assert fr.plan_release(label, installed="3.4.0", installed_release=False)["kind"] == fr.SAME
