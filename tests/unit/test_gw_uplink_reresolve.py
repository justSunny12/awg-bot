"""Перерезолв аплинка у агента шлюза (domain/gateway/reserve/uplink.py) и
его задание в планировщике агента.

Аплинк — обычный пир интерфейса AWG сервера: Endpoint в его конфиге —
имя, а ядро разрешает имя один раз, при подъёме. С резервом A-запись имени
переезжает на другой хост; без перерезолва аплинк молчал бы до перезапуска.
Поэтому: рукопожатие старое — разрешить имя заново; адрес другой —
`awg set <аплинк> peer <ключ> endpoint <адрес>:<порт>` на живом интерфейсе.

Хост подменён классом `_Host`: что отвечает `awg show`, что в конфиге
аплинка, во что разрешается имя; `awg set` пишется в журнал.

Цена ошибки: разрешение имени на каждом такте при живом аплинке — запрос
DNS раз в минуту, тот самый маячок; `awg set` при том же адресе —
лишняя пересборка сессии и потеря пакетов каждую минуту; перерезолв, когда
Endpoint — IP, или на интерфейсе, которого нет, — `awg set` в никуда и
мусор в журнале; аплинк, не переставленный после переезда, — шлюз без
связи с сервером до рестарта.
"""
from __future__ import annotations

import socket
import subprocess
import time

import pytest

from awgbot.core import config
from awgbot.domain.gateway.reserve import uplink
from awgbot.infra import gwguard

pytestmark = pytest.mark.unit

PUB = "DUMMYserverPubKey0000000000000000000000000="


class _Host:
    """Шлюз: интерфейс аплинка `awgup`, его конфиг, живой Endpoint, возраст
    рукопожатия и DNS. `sets` — вызовы `awg set`, `lookups` — разрешения."""

    def __init__(self, tmp_path, monkeypatch):
        self.iface = "awgup"
        self.endpoint_conf = "vpn.example.net:51820"
        self.live_ip = "198.51.100.7"
        self.dns = {"vpn.example.net": "198.51.100.7"}
        self.handshake_age: int | None = 600       # None — ни разу; "absent"/"unknown" — нет интерфейса/сбой
        self.exists = True
        self.awg_fails = False
        self.set_rc = 0
        self.sets: list[list[str]] = []
        self.lookups: list[str] = []
        self.dir = tmp_path
        monkeypatch.setattr(config, "GW_CONF_DIR", str(tmp_path))
        monkeypatch.setattr(config, "GW_UPLINK_IF", self.iface)
        monkeypatch.setattr(subprocess, "run", self._run)
        monkeypatch.setattr(socket, "getaddrinfo", self._gai)
        self.write_conf()

    def write_conf(self, text: str | None = None) -> None:
        if text is None:
            text = (f"[Interface]\nPrivateKey = DUMMY\nAddress = 10.8.1.9/32\n\n[Peer]\nPublicKey = {PUB}\n"
                    f"Endpoint = {self.endpoint_conf}\nAllowedIPs = 0.0.0.0/0\n")
        (self.dir / f"{self.iface}.conf").write_text(text, encoding="utf-8")

    def _gai(self, host, port, family=0, type=0, *a, **kw):
        self.lookups.append(host)
        ip = self.dns.get(host)
        if not ip:
            raise socket.gaierror("Name or service not known")
        return [(socket.AF_INET, socket.SOCK_DGRAM, 17, "", (ip, 0))]

    def _run(self, argv, *a, **kw):
        def out(text="", rc=0):
            return subprocess.CompletedProcess(argv, rc, text.encode(), b"")
        if argv[0] == "ip":
            return out('[{"ifname": "awgup"}]' if self.exists else "", 0 if self.exists else 1)
        if argv[0] != "awg":
            raise AssertionError(f"неожиданная команда {argv}")
        if argv[1] == "set":
            self.sets.append(argv[1:])
            return out(rc=self.set_rc)
        if self.awg_fails or not self.exists:
            return out(rc=1)
        what = argv[3] if len(argv) > 3 else ""
        if what == "latest-handshakes":
            ts = 0 if self.handshake_age is None else int(time.time()) - self.handshake_age
            return out(f"{PUB}\t{ts}\n")
        if what == "endpoints":
            return out(f"{PUB}\t{self.live_ip}:51820\n")
        if what == "peers":
            return out(f"{PUB}\n")
        return out()


@pytest.fixture()
def host(tmp_path, monkeypatch):
    return _Host(tmp_path, monkeypatch)


def test_a_fresh_handshake_does_not_even_resolve_the_name(host):
    host.handshake_age = 30
    assert uplink.reresolve() == "fresh"
    assert host.lookups == [] and host.sets == [], "живой аплинк — запрос DNS каждую минуту"


def test_the_same_address_is_left_alone(host):
    assert uplink.reresolve() == "same"
    assert host.lookups == ["vpn.example.net"]
    assert host.sets == [], "awg set при том же адресе"


def test_a_moved_name_sets_the_new_endpoint_on_the_live_interface(host):
    host.dns["vpn.example.net"] = "203.0.113.5"
    assert uplink.reresolve() == "set"
    assert host.sets == [["set", "awgup", "peer", PUB, "endpoint", "203.0.113.5:51820"]]


def test_a_second_pass_after_the_move_does_nothing(host):
    host.dns["vpn.example.net"] = "203.0.113.5"
    assert uplink.reresolve() == "set"
    host.live_ip = "203.0.113.5"                       # ядро встало на новый адрес
    assert uplink.reresolve() == "same"
    assert len(host.sets) == 1


def test_a_never_answering_peer_is_also_checked(host):
    """Ни одного рукопожатия (поднялись, а сервер уже переехал) — тот же
    перерезолв, а не «ждём»."""
    host.handshake_age = None
    host.dns["vpn.example.net"] = "203.0.113.5"
    assert uplink.reresolve() == "set"


def test_an_ip_endpoint_has_nowhere_to_move(host):
    host.endpoint_conf = "198.51.100.7:51820"
    host.write_conf()
    assert uplink.reresolve() == "skip:ip"
    assert host.lookups == [] and host.sets == []


@pytest.mark.parametrize("state", ["absent", "unknown"])
def test_no_interface_or_a_failing_awg_is_skipped(host, state):
    if state == "absent":
        host.exists = False
    else:
        host.awg_fails = True
    assert uplink.reresolve() == f"skip:{state}"
    assert host.lookups == [] and host.sets == []


def test_without_an_uplink_there_is_nothing_to_do(host, monkeypatch):
    monkeypatch.setattr(gwguard, "uplink_interface", lambda: "")
    assert uplink.reresolve() == "skip:no-uplink"
    assert host.sets == []


def test_an_unresolvable_name_is_skipped_without_touching_the_link(host):
    host.dns.clear()
    assert uplink.reresolve() == "skip:unresolved"
    assert host.sets == []


@pytest.mark.parametrize("text", [None, "[Interface]\nPrivateKey = DUMMY\n", "[Peer]\nEndpoint = vpn.example.net\n"])
def test_a_missing_conf_or_endpoint_is_skipped(host, text):
    if text is None:
        (host.dir / "awgup.conf").unlink()
    else:
        host.write_conf(text)
    assert uplink.reresolve() == "skip:no-endpoint"
    assert host.sets == []


def test_a_failed_awg_set_is_reported_not_raised(host):
    host.dns["vpn.example.net"] = "203.0.113.5"
    host.set_rc = 1
    assert uplink.reresolve() == "skip:set-failed"


def test_an_ipv6_endpoint_in_brackets_reads_as_an_ip(host):
    host.endpoint_conf = "[2001:db8::1]:51820"
    host.write_conf()
    assert uplink.conf_endpoint("awgup") == ("2001:db8::1", 51820)
    assert uplink.reresolve() == "skip:ip"


# ── задание в планировщике агента ────────────────────────────────────────────

async def test_the_agent_runs_the_reresolve_every_minute_with_jitter(tmp_path, monkeypatch):
    from apscheduler.triggers.interval import IntervalTrigger
    from awgbot.domain.gateway import GatewayServices
    from awgbot.infra.db import Database
    from awgbot.runtime import scheduler as sch
    monkeypatch.setattr(config, "ROLE", "gateway")
    db = Database(tmp_path / "gw.db")
    db.init_schema()
    scheduler = sch.setup_gateway_scheduler(GatewayServices(db), object())
    try:
        job = {j.id: j for j in scheduler.get_jobs()}.get("gw_uplink_reresolve")
        assert job is not None, "у агента нет задания перерезолва аплинка"
        assert isinstance(job.trigger, IntervalTrigger)
        assert job.trigger.interval.total_seconds() == 60
        assert int(job.trigger.jitter or 0) > 0, "без джиттера — маячок по расписанию"
        assert job.max_instances == 1 and job.coalesce
    finally:
        scheduler.shutdown(wait=False)
        db.close()
