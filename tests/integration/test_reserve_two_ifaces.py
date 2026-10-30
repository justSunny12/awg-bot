"""Оба интерфейса основы в зоне резерва (основа в окне переезда профилей):
процесс открытия (tools/rsvopen.py) кладёт конфиг второго интерфейса в
хранилище до `take`, называет его скрипту (`take(extra=…)` → GUEST_EXTRA),
пишет список клонов в шаг `clone_up` и делает второй интерфейс интерфейсом
переезда в app.yaml гостя; счётчики трафика гостя — сумма по всем клонам.

Окружение процесса открытия — `Box` из test_rsvopen (скрипт зоны и DNS
подменены); обёртка скрипта — подменой subprocess.run; счётчики — подменой
netns.run_in.

Цена ошибки: второго конфига нет в хранилище — скрипт не поднимет клон, и
устройства, уже переведённые на новый интерфейс, без VPN всё время переезда;
без интерфейса переезда в app.yaml гость не ведёт устройства второго
интерфейса (не создаёт, не удаляет их пиры); ключи переезда в app.yaml при
одном интерфейсе — гость ищет несуществующий интерфейс; счётчики только
первого клона — трафик переехавших устройств пропадает из учёта.
"""
from __future__ import annotations

import os
import stat
import subprocess
import types

import pytest
import yaml

from awgbot.core import config
from awgbot.infra import rsvzone
from awgbot.util import netns
from awgbot.util.rsvpkg import seal, vpn as pkgvpn
from tests.integration.test_rsvopen import LIVE, TOKEN, Box, _b64
from tests.unit.test_rsvzone import _Proc

pytestmark = pytest.mark.integration

LIVE2 = (LIVE.replace(f"PrivateKey = {_b64(1)}", f"PrivateKey = {_b64(3)}").replace("10.8.1.1/24", "10.8.7.1/24")
         .replace("ListenPort = 51820", "ListenPort = 51821").replace("10.8.1.2/32", "10.8.7.2/32")
         .replace(f"PublicKey = {_b64(2)}", f"PublicKey = {_b64(4)}"))


def _package(box: Box, ifaces) -> None:
    """Пакет с интерфейсами `ifaces` — как Box.write_package, но с выбором частей VPN."""
    vpn = pkgvpn.build(ifaces, admin=["10.8.1.2"], dns=["10.8.1.1"], upstreams=["198.51.100.53"], tz="Asia/Yekaterinburg")
    bot_part = {"db": box.replica.hex(), "conf": {"app": {"client_config": {"server_name": "Альфа"}}},
                "app_guest": {"network": {"server_host": "203.0.113.7", "server_port": 51820}},
                "env": {"BOT_TOKEN": TOKEN, "ADMIN_ID": 77}}
    blob = seal.seal({"vpn": vpn, "bot": bot_part}, k=box.k, m=box.m, pkg_gen=5, m_gen=box.m_gen, dns=None)
    path = config.DATA_DIR / "reserve" / "guest.pkg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)


@pytest.fixture()
def box(tmp_path, monkeypatch, capsys):
    b = Box(tmp_path, monkeypatch, capsys)
    real_take = b._take

    def take(conf, **kw):
        # что лежало в хранилище в момент вызова скрипта: второй конфиг обязан быть уже там
        kw["_awg_dir_at_take"] = sorted(p.name for p in (b.run_dir / "awg").glob("*.conf"))
        return real_take(conf, **kw)
    monkeypatch.setattr(rsvzone, "take", take)
    return b


def _take_call(box) -> dict:
    calls = [c for c in box.calls if c[0] == "take"]
    assert len(calls) == 1, box.calls
    return calls[0][2]


def test_two_interfaces_give_two_configs_in_the_store_before_the_take(box):
    _package(box, [("awg0", LIVE), ("awg1", LIVE2)])
    assert box.open(k=box.k.hex()) == 0, box.out
    kw = _take_call(box)
    assert kw["_awg_dir_at_take"] == ["awg1.conf"], f"конфига второго интерфейса не было к вызову скрипта: {kw}"
    assert kw["extra"] == [("awg1", "10.8.7.1/24")], kw
    assert kw["iface"] == "awg0" and kw["addr"] == "10.8.1.1/24", "первым должен идти основной интерфейс AWG"
    conf = (box.run_dir / "awg" / "awg1.conf").read_text()
    assert f"PrivateKey = {_b64(3)}" in conf and "ListenPort = 51821" in conf and "AllowedIPs = 10.8.7.2/32" in conf, conf
    assert f"PrivateKey = {_b64(1)}" not in conf, "в конфиг второго интерфейса попал ключ первого"
    assert stat.S_IMODE(os.stat(box.run_dir / "awg" / "awg1.conf").st_mode) == 0o600, "конфиг с ключом основы не 0600"


def test_the_clone_up_step_names_both_clones(box):
    _package(box, [("awg0", LIVE), ("awg1", LIVE2)])
    seen = []
    real = rsvzone.open_status_write
    box.mp.setattr(rsvzone, "open_status_write", lambda step, **f: (seen.append((step, dict(f))), real(step, **f)))
    assert box.open(k=box.k.hex()) == 0, box.out
    up = [f for s, f in seen if s == "clone_up"]
    assert up and up[0].get("ifaces") == "awg0,awg1" and up[0].get("iface") == "awg0", seen


def test_the_guest_app_yaml_makes_the_second_interface_the_migration_one(box):
    _package(box, [("awg0", LIVE), ("awg1", LIVE2)])
    assert box.open(k=box.k.hex()) == 0, box.out
    app = yaml.safe_load((box.run_dir / "conf" / "app.yaml").read_text())
    assert app["awg"]["interface"] == "awg0"
    assert (app["awg"].get("migration_interface"), app["awg"].get("migration_subnet_prefix")) == ("awg1", "10.8.7"), app["awg"]


def test_one_interface_gives_no_extra_and_no_migration_keys(box):
    _package(box, [("awg0", LIVE)])
    assert box.open(k=box.k.hex()) == 0, box.out
    kw = _take_call(box)
    assert kw["extra"] == [] and kw["_awg_dir_at_take"] == [], kw
    app = yaml.safe_load((box.run_dir / "conf" / "app.yaml").read_text())
    assert "migration_interface" not in app["awg"] and "migration_subnet_prefix" not in app["awg"], app["awg"]


def test_a_second_config_that_cannot_be_written_fails_the_opening_with_the_reason(box, monkeypatch):
    _package(box, [("awg0", LIVE), ("awg1", LIVE2)])
    real = rsvzone.write_private

    def write(path, data, *a, **kw):
        if str(path).endswith("awg1.conf"):
            raise OSError("нет места")
        return real(path, data, *a, **kw)
    monkeypatch.setattr(rsvzone, "write_private", write)
    assert box.open(k=box.k.hex()) == 1, box.out
    st = rsvzone.open_status()
    assert st["step"] == "fail" and "нет места" in st["reason"], st
    assert not [c for c in box.calls if c[0] == "take"], "скрипт зоны вызван, хотя конфиг второго клона не лёг"


# ── обёртка скрипта зоны ────────────────────────────────────────────────────

def test_take_passes_the_extra_interfaces_by_name_and_address_only(monkeypatch):
    p = _Proc(monkeypatch, out=b"take: ok")
    rsvzone.take("[Interface]\nPrivateKey = DUMMY-private-key\n", iface="awg0", addr="10.8.1.1/24", dns=[], upstreams=[],
                 admin=[], extra=[("awg1", "10.8.7.1/24"), ("awg2", "10.8.8.1/24")])
    env = p.calls[0][1]["env"]
    assert env["GUEST_EXTRA"] == "awg1=10.8.7.1/24 awg2=10.8.8.1/24", env.get("GUEST_EXTRA")
    assert not any("DUMMY" in str(v) for v in env.values()), "ключ клона в окружении скрипта"


def test_take_without_extra_clears_the_variable(monkeypatch):
    """Пустое GUEST_EXTRA — явно: значение, унаследованное окружением бота, не
    должно поднять лишний клон."""
    monkeypatch.setenv("GUEST_EXTRA", "awg9=10.9.9.1/24")
    p = _Proc(monkeypatch, out=b"take: ok")
    rsvzone.take("[Interface]\nPrivateKey = DUMMY\n", iface="awg0", addr="10.8.1.1/24", dns=[], upstreams=[], admin=[])
    assert p.calls[0][1]["env"]["GUEST_EXTRA"] == ""


# ── счётчики трафика гостя ──────────────────────────────────────────────────

@pytest.fixture()
def counters(services, monkeypatch):
    box = types.SimpleNamespace(out={}, code={}, error=None, asked=[], state={})
    monkeypatch.setattr(rsvzone, "state", lambda *a: dict(box.state))

    def run_in(path, argv, timeout=10, input_data=None, check=False):
        iface = argv[2]
        box.asked.append(iface)
        if box.error:
            raise box.error
        return subprocess.CompletedProcess(argv, box.code.get(iface, 0), box.out.get(iface, "").encode(), b"")
    monkeypatch.setattr(netns, "run_in", run_in)
    box.svc = services
    return box


def test_the_counters_are_the_sum_over_all_clones(counters):
    counters.state = {"phase": "serving", "iface": "awg0", "ifaces": "awg0 awg1"}
    counters.out = {"awg0": "pubA\t100\t200\npubB\t50\t60\n", "awg1": "pubC\t7\t8\n"}
    assert counters.svc._clone_counters() == (157, 268), "трафик второго клона не учтён"
    assert counters.asked == ["awg0", "awg1"]


def test_an_old_state_with_one_clone_reads_only_it(counters):
    counters.state = {"phase": "serving", "iface": "awg0"}
    counters.out = {"awg0": "pubA\t100\t200\n"}
    assert counters.svc._clone_counters() == (100, 200) and counters.asked == ["awg0"]


@pytest.mark.parametrize("fail", ["code", "oserror", "timeout"])
def test_one_unreadable_clone_gives_no_counters_rather_than_a_half(counters, fail):
    """Половина суммы в накопителе — следующий полный замер посчитался бы
    приростом и удвоил трафик первого клона."""
    counters.state = {"phase": "serving", "iface": "awg0", "ifaces": "awg0 awg1"}
    counters.out = {"awg0": "pubA\t100\t200\n", "awg1": "pubC\t7\t8\n"}
    if fail == "code":
        counters.code = {"awg1": 1}
    elif fail == "oserror":
        counters.error = OSError("нет nsenter")
    else:
        counters.error = subprocess.TimeoutExpired("awg", 10)
    assert counters.svc._clone_counters() == (None, None)


def test_without_clones_nothing_is_asked(counters):
    counters.state = {"phase": "serving"}
    assert counters.svc._clone_counters() == (None, None) and counters.asked == []
