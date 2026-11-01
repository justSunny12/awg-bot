"""`awg-bot firewall status` на шлюзе (tools/firewall_gw.gw_status) — строка
юнита обвязки: по коду `systemctl is-enabled` и с потолком ожидания.

Цена ошибки: systemctl, повисший на D-Bus (systemd занят, шлюз под
нагрузкой), без потолка держал команду статуса без предела — админ у консоли
шлюза не видит ни таблицы, ни SSH; systemctl, которого нет (контейнер, сломанный
PATH), ронял команду трассировкой вместо статуса; «включён» не по коду, а по
слову вывода — static/indirect юнит обвязки показывался бы «НЕ включён».
"""
from __future__ import annotations

import subprocess

import pytest

from awgbot.core import config
from awgbot.infra import gwguard
from tools import firewall_gw

pytestmark = pytest.mark.unit


@pytest.fixture()
def status(monkeypatch, capsys):
    """Всё, кроме systemctl, — в памяти; `calls` — вызовы subprocess.run."""
    monkeypatch.setattr(gwguard, "table_info", lambda: None)
    monkeypatch.setattr(gwguard, "unit_admin_ips", lambda: [])
    monkeypatch.setattr(gwguard, "read_extra", lambda: [])
    monkeypatch.setattr(gwguard, "iptables_forward_policy", lambda: "accept")
    monkeypatch.setattr(gwguard, "read_env", lambda: {})
    calls: list = []

    def run(answer):
        def fake(argv, *a, **kw):
            calls.append((argv, kw))
            if isinstance(answer, BaseException):
                raise answer
            return subprocess.CompletedProcess(argv, answer, b"", b"")
        monkeypatch.setattr(subprocess, "run", fake)
        assert firewall_gw.gw_status(None) == 0
        return [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(f"юнит {config.GW_UNIT}:")]
    run.calls = calls
    return run


@pytest.mark.parametrize("rc, word", [(0, "включён"), (1, "НЕ включён")])
def test_the_unit_line_follows_the_exit_code(status, rc, word):
    assert status(rc) == [f"юнит {config.GW_UNIT}: {word}"]
    (argv, kw), = status.calls
    assert argv == ["systemctl", "is-enabled", config.GW_UNIT]
    assert 0 < kw.get("timeout", 0) <= 10, "systemctl без потолка ожидания"


@pytest.mark.parametrize("err", [subprocess.TimeoutExpired(["systemctl"], 10), FileNotFoundError("systemctl")],
                         ids=["hung", "absent"])
def test_a_silent_systemctl_is_said_so_and_the_status_goes_on(status, err):
    assert status(err) == [f"юнит {config.GW_UNIT}: состояние не снято (systemctl не ответил)"]
