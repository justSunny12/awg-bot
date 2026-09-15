"""
Свой DNS-резолвер клиентов (docs/ROADMAP.md §3): состояние, решение админа и
связка с переездом профилей. Скрипт резолвера подменён записью в журнал —
проверяем, ЧТО бот у него просит и в каком порядке относительно рождения
двойников.
"""
from __future__ import annotations

import socket
import threading

import pytest

from awgbot.core import config, settings
from awgbot.infra import resolver

pytestmark = pytest.mark.integration


@pytest.fixture()
def fake_resolver(monkeypatch, tmp_path):
    """Скрипт резолвера → журнал вызовов; список адресов ведётся как в конфиге."""
    conf = tmp_path / "resolver.conf"
    calls: list[tuple[str, str]] = []
    fail = {"mode": ""}

    def run(mode, addr=""):
        calls.append((mode, addr))
        if fail["mode"] == mode:
            raise resolver.ResolverError("подставной отказ")
        addrs = resolver.listen_addrs(conf)
        if mode in ("install", "add") and addr not in addrs:
            addrs.append(addr)
        if mode == "remove":
            addrs = [a for a in addrs if a != addr]
        conf.write_text("bind-dynamic\n" + "".join(f"listen-address={a}\n" for a in addrs),
                        encoding="utf-8")
        return ""

    monkeypatch.setattr(resolver, "CONF_PATH", conf)
    monkeypatch.setattr(resolver, "run", run)
    # default-аргумент listen_addrs связан с боевым путём при импорте
    monkeypatch.setattr(resolver, "listen_addrs", lambda path=None: _read(conf))
    return type("R", (), {"calls": calls, "conf": conf, "fail": fail,
                          "addrs": staticmethod(lambda: _read(conf))})


def _read(conf):
    if not conf.exists():
        return []
    return [ln.split("=", 1)[1] for ln in conf.read_text(encoding="utf-8").splitlines()
            if ln.startswith("listen-address=")]


def _set_dns(monkeypatch, dns1, dns2, runtime="host"):
    monkeypatch.setattr(config, "AWG_RUNTIME", runtime)
    real = settings.get
    over = {"app.client_config.dns1": dns1, "app.client_config.dns2": dns2}
    monkeypatch.setattr(settings, "get", lambda k, d=None: over.get(k, real(k, d)))


# ── состояние и решение ──────────────────────────────────────────────────────

def test_private_dns_modes(services, monkeypatch):
    _set_dns(monkeypatch, "1.1.1.1", "1.0.0.1")
    info = services.private_dns_info()
    assert info["mode"] == "public" and info["target"].endswith(".1")
    assert services.private_dns_offer_due(), "публичный адрес и решения нет — предлагать"

    _set_dns(monkeypatch, "10.8.1.1", "10.8.1.1")
    assert services.private_dns_info()["mode"] == "private"
    assert not services.private_dns_offer_due()

    _set_dns(monkeypatch, "10.8.1.1", "1.1.1.1")
    assert services.private_dns_info()["mode"] == "public", "один публичный — утечка есть"

    _set_dns(monkeypatch, "1.1.1.1", "1.0.0.1", runtime="docker")
    assert services.private_dns_info()["mode"] == "n/a" and not services.private_dns_offer_due()


def test_any_decision_silences_the_offer_and_only_pending_drives_migration(services, monkeypatch):
    _set_dns(monkeypatch, "1.1.1.1", "1.0.0.1")
    for decision, migrates in (("pending", True), ("dismissed", False)):
        services.set_private_dns_decision(decision)
        assert not services.private_dns_offer_due()
        assert services.private_dns_for_migration() is migrates
    with pytest.raises(ValueError):
        services.set_private_dns_decision("maybe")
    _set_dns(monkeypatch, "10.8.1.1", "10.8.1.1")
    services.set_private_dns_decision("")
    assert services.private_dns_for_migration(), "уже приватный: новый интерфейс обязан получить свой"


# ── переезд ──────────────────────────────────────────────────────────────────

def test_migration_start_brings_the_resolver_up_before_twins_are_born(
        services, mig, make_active_client, fake_resolver, monkeypatch):
    """Решение «pending»: резолвер слушает <новая подсеть>.1 ДО рождения
    двойников, адрес записан в migration_dns, и конфиг двойника несёт его в
    обоих полях, а конфиг старого пира — прежний DNS."""
    from awgbot.domain import configgen
    _set_dns(monkeypatch, "1.1.1.1", "1.0.0.1")
    written: dict = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: written.__setitem__(k, v) or [k])
    services.set_private_dns_decision("pending")
    c = make_active_client(name="c", tg_id=7201)
    dc = services.add_device(c.id, "Тел")
    res = services.migration_start()
    assert res.started and res.born == 1
    assert fake_resolver.calls == [("install", "10.9.1.1")]
    assert written["app.docker.migration_dns"] == "10.9.1.1"

    monkeypatch.setattr(config, "MIGRATION_DNS", "10.9.1.1")
    assert configgen.dns_for("awg1") == ("10.9.1.1", "10.9.1.1")
    assert configgen.dns_for("awg0") == ("1.1.1.1", "1.0.0.1")
    twin = services.db.get_device(services.db.twins_by_origin()[dc.device_id])
    assert "DNS = 10.9.1.1, 10.9.1.1" in services.generate_config(twin.id)["conf"]
    assert "DNS = 1.1.1.1, 1.0.0.1" in services.generate_config(dc.device_id)["conf"]


def test_migration_start_without_a_decision_leaves_dns_alone(
        services, mig, make_active_client, fake_resolver, monkeypatch):
    _set_dns(monkeypatch, "1.1.1.1", "1.0.0.1")
    make_active_client(name="c", tg_id=7202)
    services.add_device(services.db.list_clients()[0].id, "Тел")
    services.migration_start()
    assert fake_resolver.calls == []


def test_resolver_failure_stops_the_migration_before_any_twin(
        services, mig, make_active_client, fake_resolver, monkeypatch):
    """Родить двойника с мёртвым DNS нельзя: его конфиг уедет человеку."""
    from awgbot.domain.migration import ServiceErrorMigration
    _set_dns(monkeypatch, "10.8.1.1", "10.8.1.1")
    fake_resolver.fail["mode"] = "install"
    c = make_active_client(name="c", tg_id=7203)
    services.add_device(c.id, "Тел")
    with pytest.raises(ServiceErrorMigration, match="резолвер"):
        services.migration_start()
    assert services.db.twins_by_origin() == {} and not services.migration_running()


def test_finish_switches_dns_fields_and_drops_the_old_address(
        services, mig, make_active_client, fake_resolver, monkeypatch):
    """Финал: оба поля — адрес двойников, старый адрес снят с резолвера,
    решение стёрто (оно исполнено)."""
    _set_dns(monkeypatch, "10.8.1.1", "10.8.1.1")
    fake_resolver.conf.write_text("bind-dynamic\nlisten-address=10.8.1.1\n", encoding="utf-8")
    written: dict = {}

    def set_value(k, v):
        written[k] = v
        return [k]
    monkeypatch.setattr(settings, "set_value", set_value)
    real_get = settings.get
    monkeypatch.setattr(settings, "get", lambda k, d=None: written.get(k, real_get(k, d)))
    monkeypatch.setattr(services, "_retire_interface", lambda name: None)
    services.set_private_dns_decision("pending")
    c = make_active_client(name="c", tg_id=7204)
    dc = services.add_device(c.id, "Тел")
    services.migration_start()
    twin = services.db.get_device(services.db.twins_by_origin()[dc.device_id])
    services.db.update_device_fields(twin.id, last_handshake=10 ** 9)

    removed, dropped, failed = services.migration_finish()
    assert not failed
    assert written["app.client_config.dns1"] == "10.9.1.1"
    assert written["app.client_config.dns2"] == "10.9.1.1"
    assert written["app.docker.migration_dns"] == ""
    assert ("remove", "10.8.1.1") in fake_resolver.calls
    assert fake_resolver.addrs() == ["10.9.1.1"]
    assert services.private_dns_decision() == ""


def test_cancel_drops_the_new_address_but_keeps_the_decision(
        services, mig, make_active_client, fake_resolver, monkeypatch):
    _set_dns(monkeypatch, "1.1.1.1", "1.0.0.1")
    written: dict = {}
    monkeypatch.setattr(settings, "set_value", lambda k, v: written.__setitem__(k, v) or [k])
    real_get = settings.get
    monkeypatch.setattr(settings, "get", lambda k, d=None: written.get(k, real_get(k, d)))
    services.set_private_dns_decision("pending")
    c = make_active_client(name="c", tg_id=7205)
    services.add_device(c.id, "Тел")
    services.migration_start()
    assert fake_resolver.addrs() == ["10.9.1.1"]
    services.migration_cancel()
    assert ("remove", "10.9.1.1") in fake_resolver.calls and fake_resolver.addrs() == []
    assert written["app.docker.migration_dns"] == ""
    assert services.private_dns_decision() == "pending", "решение ждёт следующего переезда"


# ── здоровье ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def udp_dns():
    """Крошечный DNS-«сервер» на 127.0.0.1: отвечает эхом заголовка с флагом QR."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    stop = threading.Event()

    def serve():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                data, peer = sock.recvfrom(512)
            except socket.timeout:
                continue
            sock.sendto(data[:2] + bytes([data[2] | 0x80]) + data[3:], peer)
    t = threading.Thread(target=serve, daemon=True); t.start()
    yield port
    stop.set(); t.join(timeout=1); sock.close()


def test_probe_talks_dns_and_times_out_on_silence(udp_dns):
    assert resolver.probe("127.0.0.1", timeout=1.0, port=udp_dns) is True
    dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); dead.bind(("127.0.0.1", 0))
    try:
        assert resolver.probe("127.0.0.1", timeout=0.3, port=dead.getsockname()[1]) is False
    finally:
        dead.close()
    assert resolver.probe("", timeout=0.1) is False


def test_health_tick_alerts_once_and_reports_recovery(services, monkeypatch):
    _set_dns(monkeypatch, "10.8.1.1", "10.8.1.1")
    alive = {"v": False}
    monkeypatch.setattr(resolver, "probe", lambda a, **k: alive["v"])
    monkeypatch.setattr(resolver, "service_active", lambda: False)
    notes = services.resolver_health_tick()
    assert len(notes) == 1 and "не отвечает" in notes[0].text and notes[0].critical
    assert services.resolver_health_tick() == [], "повторный отказ — не повторный алерт"
    alive["v"] = True
    notes = services.resolver_health_tick()
    assert len(notes) == 1 and "снова отвечает" in notes[0].text
    assert services.resolver_health_tick() == []

    _set_dns(monkeypatch, "1.1.1.1", "1.0.0.1")
    alive["v"] = False
    assert services.resolver_health_tick() == [], "публичный DNS — не наша зона"


def test_preflight_warns_when_the_private_address_is_silent(services, monkeypatch):
    from awgbot.runtime import preflight
    _set_dns(monkeypatch, "10.8.1.1", "10.8.1.1")
    monkeypatch.setattr(resolver, "probe", lambda a, **k: False)
    assert any("резолвер" in w for w in preflight._resolver_warnings(services))
    monkeypatch.setattr(resolver, "probe", lambda a, **k: True)
    assert preflight._resolver_warnings(services) == []
