"""Обёртка скрипта зоны резерва (infra/rsvzone.py): запуск режимов, разбор
состояния, приватная запись хранилища гостя, ход процесса открытия.

Цена ошибки: секрет в аргументах скрипта виден всем в `ps` на резерве (а
резерв — чужой сервер); файл хранилища, созданный с правами по умолчанию на
долю секунды, читаем чужим процессом; ненулевой код скрипта, принятый за
успех, — бот считает зону поднятой, а клона нет; битый state.json,
уронивший разбор, — файервол хоста не рисуется вовсе.
"""
from __future__ import annotations

import os
import stat
import subprocess
import types

import pytest

from awgbot.infra import rsvzone

pytestmark = pytest.mark.unit


@pytest.fixture()
def zone(tmp_path, monkeypatch):
    monkeypatch.setattr(rsvzone, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(rsvzone, "STATE_PATH", tmp_path / "state" / "state.json")
    monkeypatch.setattr(rsvzone, "OPEN_STATUS", tmp_path / "state" / "open.status")
    # путь по умолчанию у state() связан при импорте модуля — подменяем саму функцию
    orig = rsvzone.state
    monkeypatch.setattr(rsvzone, "state", lambda path=tmp_path / "state" / "state.json": orig(path))
    return tmp_path


# ── приватная запись ─────────────────────────────────────────────────────────

def test_a_private_file_is_0600_even_under_a_permissive_umask(tmp_path):
    old = os.umask(0o000)
    try:
        rsvzone.write_private(tmp_path / "d" / "secret", "DUMMY-key")
    finally:
        os.umask(old)
    path = tmp_path / "d" / "secret"
    assert path.read_text() == "DUMMY-key" and stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_a_private_write_replaces_the_old_file_whole_and_leaves_no_temporary(tmp_path):
    path = tmp_path / "rep.bin"
    rsvzone.write_private(path, b"old-old-old-old")
    rsvzone.write_private(path, b"new")
    assert path.read_bytes() == b"new", "хвост старого содержимого"
    assert os.listdir(tmp_path) == ["rep.bin"]


def test_a_failed_private_write_keeps_the_old_file_and_removes_the_temporary(tmp_path):
    path = tmp_path / "rep.bin"
    rsvzone.write_private(path, b"old")
    with pytest.raises(TypeError):
        rsvzone.write_private(path, 12345)               # не bytes и не str — запись падает посреди
    assert path.read_bytes() == b"old" and os.listdir(tmp_path) == ["rep.bin"]


# ── состояние зоны ───────────────────────────────────────────────────────────

def test_no_state_file_means_an_idle_zone(zone):
    assert rsvzone.state() == {} and rsvzone.phase() == "idle"


@pytest.mark.parametrize("text, want", [("not json", "idle"), ("[1, 2]", "idle"), ('{"phase": "dancing"}', "idle"),
                                        ('{"phase": "serving"}', "serving"), ('{"phase": "relay"}', "relay"),
                                        ('{"phase": "taking"}', "taking")])
def test_the_phase_comes_from_the_state_file_and_garbage_reads_as_idle(zone, text, want):
    (zone / "state").mkdir()
    (zone / "state" / "state.json").write_text(text)
    assert rsvzone.phase() == want


def test_the_zone_address_is_the_second_host_of_the_veth_subnet(zone):
    assert rsvzone.zone_addr() == "169.254.77.2"
    assert rsvzone.zone_addr("169.254.99.0/30") == "169.254.99.2"
    (zone / "state").mkdir()
    (zone / "state" / "state.json").write_text('{"zone_cidr": "169.254.55.4/30"}')
    assert rsvzone.zone_addr() == "169.254.55.6"
    assert rsvzone.zone_addr("не сеть") == "" and rsvzone.zone_addr("169.254.1.1/32") == ""


def test_the_open_status_roundtrips_without_secrets_and_survives_an_unwritable_directory(zone, monkeypatch):
    assert rsvzone.open_status() == {}
    rsvzone.open_status_write("clone_up", iface="awg0", port=45877)
    st = rsvzone.open_status()
    assert st["step"] == "clone_up" and st["iface"] == "awg0" and st["port"] == "45877" and st["at"]
    assert os.listdir(zone / "state") == ["open.status"]
    monkeypatch.setattr(rsvzone, "STATE_DIR", zone / "state" / "open.status" / "nope")
    rsvzone.open_status_write("fail", reason="x")        # не падает


# ── запуск режимов ───────────────────────────────────────────────────────────

class _Proc:
    def __init__(self, monkeypatch, rc=0, out=b"", err=b"", exc=None):
        self.calls = []
        self.rc, self.out, self.err, self.exc = rc, out, err, exc

        def fake(args, **kw):
            self.calls.append((args, kw))
            if self.exc:
                raise self.exc
            return types.SimpleNamespace(returncode=self.rc, stdout=self.out, stderr=self.err)
        monkeypatch.setattr(subprocess, "run", fake)


def test_a_mode_runs_the_script_with_bash_and_returns_its_text(monkeypatch):
    p = _Proc(monkeypatch, out=b"shell: ok\n")
    assert rsvzone.run("--shell") == "shell: ok"
    args, kw = p.calls[0]
    assert args[0] == "bash" and args[1].endswith("install/reserve-zone-setup.sh") and args[2:] == ["--shell"]
    assert rsvzone.run("take") and p.calls[1][0][2] == "--take", "режим без дефисов дополняется"


def test_a_nonzero_exit_is_a_zone_error_with_the_tail_of_the_output(monkeypatch):
    _Proc(monkeypatch, rc=1, out=b"a\nb\n", err="ОШИБКА: клон не создался\nc\nd\n".encode())
    with pytest.raises(rsvzone.ZoneError) as e:
        rsvzone.run("take")
    assert "клон не создался" in str(e.value) and "a" not in str(e.value).split("\n")[:1]
    _Proc(monkeypatch, rc=3)
    with pytest.raises(rsvzone.ZoneError, match="код 3"):
        rsvzone.run("take")


def test_warnings_of_a_successful_mode_go_to_the_journal_and_the_rest_is_debug(monkeypatch, caplog):
    """Код 0, а в выводе «ВНИМАНИЕ» (NETMAP не встал, клон окна не поднялся, нет
    conntrack): вывод --take никто не пересказывает — без строки здесь
    предупреждения скрипта терялись бы все."""
    import logging
    _Proc(monkeypatch, out="ВНИМАНИЕ: nft не принял NETMAP подсетей клонов — …\ntake: ok\n".encode(),
          err="ВНИМАНИЕ: конфига awg1 нет — интерфейс не поднят\n".encode())
    caplog.set_level(logging.DEBUG, logger="awgbot.infra.rsvzone")
    assert rsvzone.run("take").endswith("интерфейс не поднят")
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warns == ["резерв: зона --take: ВНИМАНИЕ: nft не принял NETMAP подсетей клонов — …",
                     "резерв: зона --take: ВНИМАНИЕ: конфига awg1 нет — интерфейс не поднят"], warns
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert debug == ["резерв: зона --take: take: ok"], debug


@pytest.mark.parametrize("exc", [OSError("нет bash"), subprocess.TimeoutExpired("bash", 1)])
def test_a_script_that_cannot_start_or_hangs_is_a_zone_error(monkeypatch, exc):
    _Proc(monkeypatch, exc=exc)
    with pytest.raises(rsvzone.ZoneError, match="не запустился"):
        rsvzone.run("wipe")


def test_the_clone_config_goes_through_stdin_only(monkeypatch):
    p = _Proc(monkeypatch, out=b"take: ok")
    conf = "[Interface]\nPrivateKey = DUMMY-private-key\nListenPort = 45877\n"
    rsvzone.take(conf, iface="awg0", addr="10.8.1.1/24", dns=["10.8.1.1", "10.255.53.53"], upstreams=["198.51.100.53"],
                 admin=["10.8.1.2"], channel_port=8789)
    args, kw = p.calls[0]
    assert kw["input"] == conf.encode()
    assert "DUMMY-private-key" not in " ".join(args), "ключ клона в аргументах скрипта"
    assert not any("DUMMY-private-key" in str(v) for v in kw["env"].values()), "ключ клона в окружении скрипта"
    env = kw["env"]
    assert (env["GUEST_IF"], env["GUEST_ADDR"], env["GUEST_DNS"], env["GUEST_UPSTREAMS"], env["GUEST_ADMIN"],
            env["GUEST_CHANNEL_PORT"]) == ("awg0", "10.8.1.1/24", "10.8.1.1 10.255.53.53", "198.51.100.53", "10.8.1.2", "8789")


def test_without_a_channel_port_the_script_default_stays(monkeypatch):
    p = _Proc(monkeypatch)
    monkeypatch.delenv("GUEST_CHANNEL_PORT", raising=False)
    rsvzone.take("[Interface]\nPrivateKey = DUMMY\n", iface="awg0", addr="10.8.1.1/24", dns=[], upstreams=[], admin=[])
    assert "GUEST_CHANNEL_PORT" not in p.calls[0][1]["env"]


@pytest.mark.parametrize("port, want", [(443, "443"), (8443, "8443"), (0, ""), (None, "")])
def test_serve_passes_the_webhook_port_for_the_host_firewall(monkeypatch, port, want):
    p = _Proc(monkeypatch)
    rsvzone.serve(port)
    assert p.calls[0][1]["env"]["WEBHOOK_PORT"] == want


def test_relay_passes_the_address_of_the_main_for_the_tail(monkeypatch):
    p = _Proc(monkeypatch)
    rsvzone.relay("203.0.113.5:45877")
    assert p.calls[0][1]["env"]["RELAY_TO"] == "203.0.113.5:45877" and p.calls[0][0][2] == "--relay"


def test_plan_and_status_parse_key_value_lines_and_report_an_error_instead_of_raising(monkeypatch):
    _Proc(monkeypatch, out=b"store=tmpfs-noswap\ncake=yes\nwebhook_port=443\nzone=absent\n")
    assert rsvzone.plan() == {"store": "tmpfs-noswap", "cake": "yes", "webhook_port": "443", "zone": "absent"}
    _Proc(monkeypatch, rc=1, err=b"boom")
    assert rsvzone.status() == {"error": "boom"} and rsvzone.plan() == {"error": "boom"}


def test_reading_json_of_the_store_survives_garbage(tmp_path):
    p = tmp_path / "w.json"
    assert rsvzone.read_json(p) == {}
    p.write_text("{oops")
    assert rsvzone.read_json(p) == {}
    p.write_text("[1]")
    assert rsvzone.read_json(p) == {}
    p.write_text('{"port": 443}')
    assert rsvzone.read_json(p) == {"port": 443}
