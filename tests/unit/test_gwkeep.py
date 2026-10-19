"""
domain/gwkeep.py — закрытый список копии настроек шлюза: сверен с ключами
разделов агента и с firewall.env без SSH_PORT; чужое отброшено; отпечаток
стабилен к порядку и не зависит от traffic; своя почта в блоке — только при
памятке «оставить своё».
"""
from __future__ import annotations

import pytest

from awgbot.domain import gwkeep

pytestmark = pytest.mark.unit


def test_conf_keys_are_the_keys_of_the_agent_sections():
    """Расписания обновлений у агента нет (шлюзы обновляет сервер AWG) — ключа
    в закрытом списке тоже; сервер с прежним блоком его отбросит (clean)."""
    from awgbot.bot.sections import mon, notify
    want = set(notify.KEYS) | {k for k in mon.KEYS if k.startswith("app.gateway.")
                               or k == "app.monitoring.alert_streak"}
    assert set(gwkeep.CONF_KEYS) == want and "updates.poll_schedule" not in gwkeep.CONF_KEYS
    assert gwkeep.clean({"conf": {"updates.poll_schedule": "week"}})["conf"] == {}


def test_fw_keys_are_firewall_env_without_the_ssh_port_and_resolved_addresses():
    """SSH_PORT — факт устройства; SSH_ALLOW_RESOLVED — факт резолвера: оба
    вне отпечатка и вне применения."""
    from awgbot.infra.gwguard.common import ENV_KEYS
    assert set(gwkeep.FW_KEYS) == set(ENV_KEYS) - {"SSH_PORT", "SSH_ALLOW_RESOLVED"}
    a = {"conf": {}, "fw": {"SSH_ALLOW": "home.example.org", "SSH_ALLOW_RESOLVED": "198.51.100.4"}}
    b = {"conf": {}, "fw": {"SSH_ALLOW": "home.example.org", "SSH_ALLOW_RESOLVED": "198.51.100.9"}}
    assert gwkeep.print_(a) == gwkeep.print_(b), "сменился адрес имени — блок «другой»"
    assert "SSH_ALLOW_RESOLVED" not in gwkeep.clean(a)["fw"]


def test_every_key_has_a_group_for_the_question():
    grouped = {k for _name, keys in gwkeep.GROUPS for k in keys}
    assert set(gwkeep.CONF_KEYS) | {f"fw:{k}" for k in gwkeep.FW_KEYS} | {"mail"} == grouped


def test_foreign_and_wrong_values_are_dropped():
    got = gwkeep.clean({"conf": {"quiet_hours.quiet_hours_start": 22, "evil.key": 1,
                                 "app.gateway.temp_alert_c": [1, 2], "updates.poll_schedule": "x" * 999},
                        "fw": {"SSH_FILTER": "1", "SSH_PORT": "2222", "SSH_ALLOW": "1.2.3.4; rm -rf /"},
                        "mail": {"login": "a@b.c", "password": "DUMMY"}, "extra": {"x": 1}})
    assert got == {"conf": {"quiet_hours.quiet_hours_start": 22}, "fw": {"SSH_FILTER": "1"}, "mail": None}
    assert gwkeep.clean("мусор") == {"conf": {}, "fw": {}, "mail": None}


def test_the_print_ignores_order_and_traffic():
    a = {"conf": {"quiet_hours.quiet_hours_start": 22, "quiet_hours.quiet_hours_end": 7}, "fw": {"SSH_FILTER": "1"}}
    b = {"fw": {"SSH_FILTER": "1"}, "conf": {"quiet_hours.quiet_hours_end": 7, "quiet_hours.quiet_hours_start": 22}}
    assert gwkeep.print_(a) == gwkeep.print_(b) and len(gwkeep.print_(a)) == 16
    t1 = gwkeep.block(a, at="A", traffic={"month": "2026-10", "rx": 1, "tx": 2})
    t2 = gwkeep.block(a, at="B", traffic={"month": "2026-10", "rx": 9, "tx": 9})
    assert t1["hash"] == t2["hash"]
    assert gwkeep.print_(a) != gwkeep.print_({**a, "fw": {"SSH_FILTER": "0"}})


def test_traffic_continues_only_within_the_same_month():
    acc = {"month": "2026-10", "rx": 5, "tx": 5, "last_rx": 100, "last_tx": 100}
    merged = gwkeep.traffic_merge(acc, {"month": "2026-10", "rx": 10, "tx": 20}, "2026-10")
    assert (merged["rx"], merged["tx"], merged["last_rx"]) == (15, 25, 100)
    assert gwkeep.traffic_merge(acc, {"month": "2026-09", "rx": 10, "tx": 20}, "2026-10") == acc
    assert gwkeep.traffic_clean({"month": "2026-10", "rx": -1, "tx": 0}) is None


def test_the_differing_groups_are_named_in_order():
    a = {"conf": {"quiet_hours.quiet_hours_start": 22}, "fw": {"SSH_ALLOW": "203.0.113.5"}}
    b = {"conf": {"quiet_hours.quiet_hours_start": 23}, "fw": {"SSH_ALLOW": "198.51.100.7"}}
    assert gwkeep.differs(a, b) == ["тихие часы", "адреса SSH"]


def test_own_mail_goes_into_the_block_only_with_the_memo(tmp_path, monkeypatch):
    from awgbot.domain.gateway import GatewayServices
    from awgbot.infra import gwguard
    from awgbot.infra.db import Database
    monkeypatch.setattr(gwguard, "FW_ENV", str(tmp_path / "firewall.env"))
    db = Database(str(tmp_path / "gw.db")); db.init_schema()
    try:
        s = GatewayServices(db)
        monkeypatch.setattr(s, "email_account", lambda: type("A", (), {
            "login": "own@example.org", "password": "DUMMY", "imap_host": "imap.example.org", "imap_port": 993,
            "smtp_host": "smtp.example.org", "smtp_port": 587})())
        assert s.keep_collect()["mail"] is None, "почта с сервера ушла как своя"
        db.set_state(s._CFG_KEEP_KEY, '{"mail": "server-print"}')
        mail = s.keep_collect()["mail"]
        assert mail["login"] == "own@example.org" and mail["keep"] == "server-print"
    finally:
        db.close()
