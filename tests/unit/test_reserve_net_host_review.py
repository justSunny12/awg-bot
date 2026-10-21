"""Сеть резерва после ревью — части без БД: файервол хоста (infra/nftguard:
ответы в зону в NAT-only, DNAT хвоста только с внешнего интерфейса, бот основы
в зоне без частной сети провайдера, файл правил не опережает ядро), шлюз
(infra/gwguard: пересылка к резерву открыта — по файлу состояния скрипта;
мост br0 — обычный интерфейс локальной сети, в Python и в скрипте обвязки),
маршрут слота (`ensure_slot_policy(..., via=)`), сокет локального канала
(guestctl: 0600 с создания).

Цена ошибки: в NAT-only без пропуска ответов — drop на вход в зону режет
ответы на соединения самого бота основы (Telegram, DNS), переезд «прошёл», а
бот немой; DNAT хвоста со всех интерфейсов — клиенты самого резерва к тем же
портам уходят на основу; бот основы в зоне с доступом в частную сеть
провайдера — чужой код сканирует соседей резерва; файл правил с текстом,
отвергнутым ядром, — сверка считает «ok», а после ребута грузится то, что
ядро не приняло; состояние пересылки через скрипт каждые 5 с — fork на каждом
такте; br0 отброшен как «мост Docker» — на шлюзе с мостом локальной сети нет
вовсе; маршрут без via через veth — ARP на каждый адрес, зонд запасного слота
мёртв; сокет, созданный 0755 и зажатый chmod позже, — окно, в которое любой
локальный процесс подключается к каналу гостя.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import types
from pathlib import Path

import pytest

from awgbot.infra import gwguard, nftguard
from awgbot.infra.gwguard import rsv as gwrsv
from awgbot.infra.routing import base as rbase, slots

from tests.conftest import SH
from tests.unit.test_nftguard_guest_rf import RF, _chain, _lines, _spec
from tests.unit.test_reserve_gw_script import CONF, _Gw

pytestmark = pytest.mark.unit

VETHS = 'oifname { "awggv-h", "awggr-h" } ct state established,related accept'


# ── nftguard: ответы в зону в NAT-only ──────────────────────────────────────

@pytest.mark.parametrize("phase", ["taking", "serving"])
def test_nat_only_lets_replies_into_the_zone_before_any_drop(phase):
    fwd = _chain(nftguard.render(_spec(filter=False, **{**RF, "guest_phase": phase})), "forward")
    assert VETHS in fwd, "\n".join(fwd)
    first_drop = min(i for i, ln in enumerate(fwd) if ln.endswith(" drop") and ("awggv-h" in ln or "awggr-h" in ln))
    assert fwd.index(VETHS) < first_drop, "пропуск ответов стоит после drop на вход в зону — ответы режутся"


def test_with_the_filter_the_common_established_rule_covers_it():
    fwd = _chain(nftguard.render(_spec(filter=True, **RF)), "forward")
    assert VETHS not in fwd, "в режиме фильтра общий established accept наверху — второй не нужен"


@pytest.mark.parametrize("phase", ["", "idle", "relay"])
def test_without_a_hosted_zone_there_is_no_door_into_the_veths(phase):
    text = nftguard.render(_spec(filter=False, **{**RF, "guest_phase": phase}))
    assert VETHS not in _lines(text), "дверь в veth зоны без зоны"


# ── nftguard: DNAT хвоста только с внешнего интерфейса ──────────────────────

RELAY = dict(guest_phase="relay", relay_to="203.0.113.5:45877", relay_udp_port=45877)


def _pre(**kw) -> list[str]:
    return _chain(nftguard.render(_spec(**kw)), "prerouting")


def test_the_tail_dnat_is_bound_to_the_wan_interfaces():
    pre = _pre(wan_ifs=["eth0", "ens3"], **RELAY)
    assert 'iifname { "eth0", "ens3" } udp dport 45877 counter dnat ip to 203.0.113.5:45877' in pre, pre
    assert not [ln for ln in pre if "dnat ip to 203.0.113.5" in ln and "iifname" not in ln], \
        "DNAT хвоста со всех интерфейсов — клиенты резерва к тому же порту уходят на основу"


def test_the_tail_dnat_of_several_ports_is_bound_to_the_wan_too():
    pre = _pre(wan_ifs=["eth0"], relay_udp_ports=[45877, 45878], **RELAY)
    assert 'iifname { "eth0" } udp dport { 45877, 45878 } counter dnat ip to 203.0.113.5' in pre, pre


def test_without_known_wan_interfaces_the_tail_dnat_still_works():
    """Внешний интерфейс не определился (нет маршрута по умолчанию) — хвост
    важнее: правило без привязки, как раньше."""
    assert "udp dport 45877 counter dnat ip to 203.0.113.5:45877" in _pre(**RELAY)


# ── nftguard: бот основы в зоне — без частной сети провайдера ───────────────

def _private_drop(**kw) -> str:
    lines = [ln for ln in _lines(nftguard.render(_spec(wan_ifs=["eth0"], **kw))) if "ip daddr { 10.0.0.0/8" in ln
             and ln.endswith(" drop") and 'oifname { "eth0" }' in ln]
    assert len(lines) == 1, lines
    return lines[0]


@pytest.mark.parametrize("phase", ["taking", "serving"])
def test_the_zone_veth_cannot_reach_private_addresses_of_the_wan(phase):
    assert '"awggv-h"' in _private_drop(**{**RF, "guest_phase": phase})


@pytest.mark.parametrize("phase", ["idle", "relay"])
def test_without_a_hosted_zone_the_veth_is_not_in_the_private_drop(phase):
    assert "awggv-h" not in _private_drop(**{**RF, "guest_phase": phase})


# ── nftguard: файл правил не опережает ядро ─────────────────────────────────

@pytest.fixture()
def rules(tmp_path, monkeypatch):
    d = tmp_path / "nftables.d"
    monkeypatch.setattr(nftguard, "RULES_DIR", str(d))
    monkeypatch.setattr(nftguard, "RULES_FILE", str(d / "awg-bot-guard.nft"))
    state = types.SimpleNamespace(load_fails=False, loaded=[])

    def nft(args, timeout=10, check=True):
        if args[:1] == ["-f"]:
            if state.load_fails:
                raise nftguard.GuardError("nft -f: Error: Could not process rule")
            state.loaded.append(open(args[1], encoding="utf-8").read())
        return subprocess.CompletedProcess(args, 0, b"", b"")
    monkeypatch.setattr(nftguard, "_nft", nft)
    state.file = d / "awg-bot-guard.nft"
    return state


def test_a_text_the_kernel_refused_gives_the_previous_file_back(rules):
    nftguard.apply_text("table inet awg_bot_guard { # DUMMY old }\n")
    rules.load_fails = True
    with pytest.raises(nftguard.GuardError):
        nftguard.apply_text("table inet awg_bot_guard { # DUMMY new }\n")
    assert rules.file.read_text(encoding="utf-8") == "table inet awg_bot_guard { # DUMMY old }\n", \
        "в файле текст, который ядро не приняло — сверка «ok», а после ребута грузится он"


def test_a_text_the_kernel_refused_on_the_first_apply_leaves_no_file(rules):
    """Прежнего файла нет (первое применение) — отвергнутый ядром текст тоже
    не должен остаться в файле: после ребута nftables загрузил бы именно его."""
    rules.load_fails = True
    with pytest.raises(nftguard.GuardError):
        nftguard.apply_text("table inet awg_bot_guard { # DUMMY new }\n")
    assert not rules.file.exists() or rules.file.read_text(encoding="utf-8") == "", \
        f"файл правил опередил ядро: {rules.file.read_text(encoding='utf-8')!r}"


def test_an_accepted_text_is_written_and_loaded(rules):
    nftguard.apply_text("table inet awg_bot_guard { # DUMMY }\n")
    assert rules.file.read_text(encoding="utf-8") == rules.loaded[-1] == "table inet awg_bot_guard { # DUMMY }\n"


# ── шлюз: пересылка к резерву открыта — по файлу состояния ─────────────────

def test_is_open_reads_the_state_file_written_by_the_script(tmp_path, monkeypatch):
    gw = _Gw(tmp_path)
    monkeypatch.setattr(gwrsv, "STATE_PATH", gw.state)
    called = []
    monkeypatch.setattr(gwrsv, "run", lambda *a, **k: called.append(a) or "")
    assert gwrsv.is_open() is False, "файла нет — закрыто"
    src = tmp_path / "in.conf"
    src.write_text(CONF, encoding="utf-8")
    assert gw.run("--up", str(src)).returncode == 0
    assert gw.run("--open", "198.51.100.0/24").returncode == 0
    assert gwrsv.is_open() is True, gw.state.read_text(encoding="utf-8")
    assert gw.run("--close").returncode == 0
    assert gwrsv.is_open() is False
    assert not called, "is_open зовёт скрипт — fork на каждом такте"


@pytest.mark.parametrize("text, want", [("open=1\nsrc=x\n", True), ("open=0\n", False), ("src=x\n", False),
                                        ("", False), ("open= 1 \n", True), ("open=yes\n", False)])
def test_is_open_takes_only_open_equals_one(tmp_path, monkeypatch, text, want):
    p = tmp_path / "rsv.status"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setattr(gwrsv, "STATE_PATH", p)
    assert gwrsv.is_open() is want


# ── шлюз: br0 — обычный интерфейс локальной сети ────────────────────────────

ADDRS = [{"ifname": "lo", "addr_info": [{"local": "127.0.0.1"}]},
         {"ifname": "docker0", "addr_info": [{"local": "192.168.68.1"}]},
         {"ifname": "br-3f2a", "addr_info": [{"local": "192.168.68.2"}]},
         {"ifname": "awg0", "addr_info": [{"local": "192.168.68.3"}]},
         {"ifname": "veth9", "addr_info": [{"local": "192.168.68.4"}]},
         {"ifname": "br0", "addr_info": [{"local": "192.168.68.222"}]}]


def test_a_bridge_br0_is_the_lan_interface_and_docker_bridges_are_not(monkeypatch):
    monkeypatch.setattr(gwguard.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(ADDRS).encode(), b""))
    assert gwguard.iface_for_subnet("192.168.68.0/24") == ("br0", "192.168.68.222"), \
        "мост br0 отброшен — на шлюзе с мостом локальной сети нет"


def test_the_setup_script_finds_br0_too(tmp_path):
    script = (Path(__file__).resolve().parents[2] / "install" / "routing-gw-setup.sh").read_text(encoding="utf-8")
    fn = re.search(r"^lan_iface_for\(\) \{.*?^\}$", script, re.S | re.M).group(0)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ip = bin_dir / "ip"
    ip.write_text("#!/bin/sh\ncat <<'EOF'\n"
                  "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
                  "3: docker0    inet 192.168.68.1/24 scope global docker0\\       valid_lft forever\n"
                  "4: br-3f2a    inet 192.168.68.2/24 scope global br-3f2a\\       valid_lft forever\n"
                  "6: br0    inet 192.168.68.222/24 brd 192.168.68.255 scope global br0\\       valid_lft forever\n"
                  "EOF\n", encoding="utf-8")
    ip.chmod(0o755)
    r = subprocess.run([SH, "-c", fn + "\nlan_iface_for 192.168.68.0/24"], capture_output=True, text=True,
                       env={"PATH": f"{bin_dir}:/usr/bin:/bin"}, timeout=20)
    assert r.stdout.strip() == "br0 192.168.68.222", f"скрипт и бот разошлись в интерфейсе: {r.stdout!r} {r.stderr}"


# ── маршрут слота с via ─────────────────────────────────────────────────────

@pytest.fixture()
def host_cmds(monkeypatch):
    calls: list[list[str]] = []

    def host(argv, check=True, timeout=10):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(rbase, "_host", host)
    return calls


def test_a_slot_route_through_a_veth_goes_via_the_host_address(host_cmds):
    slots.ensure_slot_policy(9, "awggr-n", via="169.254.77.5")
    route = host_cmds[0]
    assert route[:6] == ["ip", "route", "replace", "default", "via", "169.254.77.5"] and route[6:8] == ["dev", "awggr-n"], route


def test_a_p2p_link_route_has_no_via(host_cmds):
    slots.ensure_slot_policy(1, "awglink")
    assert "via" not in host_cmds[0], host_cmds[0]


# ── guestctl: сокет 0600 с создания ─────────────────────────────────────────

async def test_the_guest_socket_is_private_from_its_creation(tmp_path, monkeypatch):
    """Права смотрим сразу после bind — до chmod, который идёт следом."""
    from awgbot.runtime.pairlink import guestctl
    seen = {}
    real = asyncio.start_unix_server

    async def spy(cb, path, **kw):
        srv = await real(cb, path=path, **kw)
        seen["mode"] = stat.S_IMODE(os.stat(path).st_mode)
        return srv
    monkeypatch.setattr(guestctl.asyncio, "start_unix_server", spy)
    before = os.umask(0o022)
    try:
        d = tempfile.mkdtemp(prefix="rg", dir="/tmp")         # путь сокета короче 104 символов
        srv = guestctl.GuestCtlServer(types.SimpleNamespace(), os.path.join(d, "g.sock"))
        await srv.ensure()
        try:
            assert seen["mode"] & 0o077 == 0, f"сокет создан {oct(seen['mode'])} — окно до chmod"
            now = os.umask(0o022)
            assert now == 0o022, f"umask процесса не вернулся: {oct(now)}"
        finally:
            await srv.stop()
            shutil.rmtree(d, ignore_errors=True)
    finally:
        os.umask(before)
