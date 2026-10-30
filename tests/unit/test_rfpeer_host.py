"""РФ-доступ через второй сервер на хосте (infra/routing/rfpeer): файл согласия
для обвязки файервола, адреса линка пары из его конфига, правило 151 у
транзитного, `AllowedIPs` пира линка пары у просящего, рукопожатие линка пары.

Ядро подменено классом-состоянием `_Kernel` (ip rule, `awg show/set awgpair`)
на месте `subprocess` модуля запуска команд: ни одного настоящего вызова.

Цена ошибки: правило 151 дважды — `ip rule add` каждый такт копит дубли, а
снятие одной копии оставляет транзит открытым после отзыва согласия; правило
не снимается целиком — пакеты второй стороны идут в таблицу РФ-доступа без
согласия; `awg set` на каждом такте без изменений — тысячи вызовов в сутки и
дрожание пира; широкие `AllowedIPs` не сужаются — после пропажи слота
вторая сторона по-прежнему может прислать пакет с любым адресом; рукопожатие
«0» сочтено живым — слот на мёртвом линке, РФ-трафик в никуда; файл согласия
не атомарно — CLI читает половину и рисует таблицу без транзита.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import types

import pytest

from awgbot.core import config
from awgbot.infra.routing import base
from awgbot.infra.routing import rfpeer as rfp

pytestmark = pytest.mark.unit

PUB = "DUMMY-PEER-PUB="
PEER4 = "10.99.98.1"
CONF = (f"[Interface]\nAddress = 10.99.98.2/30\nListenPort = 47101\nPrivateKey = DUMMY-PRIV=\n\n"
        f"[Peer]\nPublicKey = {PUB}\nPresharedKey = DUMMY-PSK=\nAllowedIPs = {PEER4}/32\n"
        "PersistentKeepalive = 25-35\n")


class _Kernel:
    """Ядро хоста для команд модуля: правила (`ip rule`), пир линка пары
    (`awg show/set awgpair`). `missing` — бинари, которых нет; `hang` — бинари,
    которые не отвечают; `down` — интерфейса awgpair нет. `calls` — журнал."""

    def __init__(self):
        self.rules: list[str] = []
        self.allowed: dict[str, list[str]] = {PUB: [f"{PEER4}/32"]}
        self.handshake: dict[str, int] = {PUB: 0}
        self.missing: set[str] = set()
        self.hang: set[str] = set()
        self.down = False
        self.calls: list[list[str]] = []

    def run(self, args, input=None, stdout=None, stderr=None, timeout=None):
        args = list(args)
        self.calls.append(args)
        if args[0] in self.missing:
            raise FileNotFoundError(args[0])
        if args[0] in self.hang:
            raise subprocess.TimeoutExpired(args, timeout)
        out, rc, err = "", 0, ""
        if args[:3] == ["ip", "rule", "show"]:
            out = "0:\tfrom all lookup local\n" + "".join(f"{r}\n" for r in self.rules) + "32766:\tfrom all lookup main\n"
        elif args[:3] == ["ip", "rule", "add"]:
            self.rules.append(self._rule_text(args[3:]))
        elif args[:3] == ["ip", "rule", "del"]:
            text = self._rule_text(args[3:])
            if text in self.rules:
                self.rules.remove(text)
            else:
                rc, err = 2, "RTNETLINK answers: No such file or directory"
        elif args[:2] == ["awg", "show"] and self.down:
            rc, err = 1, "Unable to access interface: No such device"
        elif args[:4] == ["awg", "show", "awgpair", "allowed-ips"]:
            out = "".join(f"{p}\t{' '.join(ips) or '(none)'}\n" for p, ips in self.allowed.items())
        elif args[:4] == ["awg", "show", "awgpair", "latest-handshakes"]:
            out = "".join(f"{p}\t{ts}\n" for p, ts in self.handshake.items())
        elif args[:3] == ["awg", "set", "awgpair"] and self.down:
            rc, err = 1, "Unable to modify interface: No such device"
        elif args[:3] == ["awg", "set", "awgpair"]:
            pub, ips = args[4], args[6]
            self.allowed[pub] = ips.split(",")
        return subprocess.CompletedProcess(args, rc, out.encode(), err.encode())

    @staticmethod
    def _rule_text(spec: list[str]) -> str:
        """`ip rule show`: «151:\tfrom 10.99.98.1 iif awgpair lookup 100»."""
        kv = dict(zip(spec[::2], spec[1::2]))
        return f"{kv['pref']}:\tfrom {kv['from']} iif {kv['iif']} lookup {kv['lookup']}"

    def sets(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["awg", "set"]]

    def adds(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["ip", "rule", "add"]]


@pytest.fixture()
def k(tmp_path, monkeypatch):
    kern = _Kernel()
    monkeypatch.setattr(base, "subprocess", types.SimpleNamespace(
        run=kern.run, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired))
    awg = tmp_path / "awg"
    awg.mkdir()
    (awg / "awgpair.conf").write_text(CONF, encoding="utf-8")
    monkeypatch.setattr(config, "AWG_DIR", str(awg))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    kern.awg = awg
    return kern


# ── файл согласия ───────────────────────────────────────────────────────────

def test_the_consent_file_round_trips_and_is_private(k):
    rfp.write(True, False, "2026-11-02T14:05:00")
    assert rfp.read() == {"offer": True, "slot": False, "at": "2026-11-02T14:05:00"}
    p = rfp.rfpeer_path()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600, "файл согласия читается всеми"
    assert stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700
    assert not os.path.exists(str(p) + ".tmp"), "временный файл остался — запись не атомарна"
    rfp.write(False, True)
    assert rfp.read() == {"offer": False, "slot": True, "at": ""}, "перезапись не заменила файл"


def test_no_file_broken_file_or_a_list_read_as_no_consent(k):
    assert rfp.read() == {}
    rfp.rfpeer_path().parent.mkdir(parents=True)
    rfp.rfpeer_path().write_text("{оборвано", encoding="utf-8")
    assert rfp.read() == {}
    rfp.rfpeer_path().write_text(json.dumps([1, 2]), encoding="utf-8")
    assert rfp.read() == {}


def test_remove_is_quiet_when_there_is_nothing(k):
    rfp.remove()
    rfp.write(True, True)
    rfp.remove()
    rfp.remove()
    assert rfp.read() == {}


# ── адреса линка пары ───────────────────────────────────────────────────────

def test_pair_conf_gives_own_address_peer_address_and_key(k):
    assert rfp.pair_conf() == {"self4": "10.99.98.2", "peer4": PEER4, "pub": PUB}


def test_without_the_pair_config_there_is_nothing(k):
    (k.awg / "awgpair.conf").unlink()
    assert rfp.pair_conf() == {}


# ── правило 151 (транзитный) ────────────────────────────────────────────────

def test_the_rule_is_added_once_however_often_it_is_ensured(k):
    for _ in range(3):
        rfp.ensure_rule(PEER4)
    assert k.rules == [f"151:\tfrom {PEER4} iif awgpair lookup {config.ROUTING_TABLE}"], k.rules
    assert len(k.adds()) == 1, "правило добавляется на каждом вызове — дубли в ядре"
    assert rfp.rule_present(PEER4)


def test_drop_removes_every_copy_and_is_quiet_without_any(k):
    rule = f"151:\tfrom {PEER4} iif awgpair lookup {config.ROUTING_TABLE}"
    k.rules = [rule, rule, "150:\tfrom 169.254.77.6 iif awggr-h lookup 100"]
    rfp.drop_rule(PEER4)
    assert k.rules == ["150:\tfrom 169.254.77.6 iif awggr-h lookup 100"], \
        "снята одна копия — транзит остался открыт (или задето правило зоны)"
    rfp.drop_rule(PEER4)
    assert not rfp.rule_present(PEER4)


def test_the_rule_of_another_address_is_not_taken_for_ours(k):
    k.rules = [f"151:\tfrom 10.99.98.9 iif awgpair lookup {config.ROUTING_TABLE}"]
    assert not rfp.rule_present(PEER4)
    rfp.ensure_rule(PEER4)
    assert len(k.rules) == 2


def test_no_ip_binary_is_unavailable_and_a_hang_is_an_error(k):
    k.missing.add("ip")
    with pytest.raises(base.RoutingUnavailable):
        rfp.ensure_rule(PEER4)
    k.missing.clear()
    k.hang.add("ip")
    with pytest.raises(base.RoutingError):
        rfp.ensure_rule(PEER4)


# ── AllowedIPs (просящий) ───────────────────────────────────────────────────

def test_widening_adds_the_default_route_once(k):
    assert rfp.allowed_ips_wide(True) is True
    assert k.allowed[PUB] == [f"{PEER4}/32", "0.0.0.0/0"]
    assert k.sets()[-1] == ["awg", "set", "awgpair", "peer", PUB, "allowed-ips", f"{PEER4}/32,0.0.0.0/0"]
    n = len(k.sets())
    assert rfp.allowed_ips_wide(True) is False
    assert len(k.sets()) == n, "без изменений — снова awg set"


def test_narrowing_goes_back_to_the_peer_address_only(k):
    k.allowed[PUB] = ["0.0.0.0/0", f"{PEER4}/32"]
    assert rfp.allowed_ips_wide(True) is False and k.sets() == [], "порядок адресов в выводе — не изменение"
    assert rfp.allowed_ips_wide(False) is True
    assert k.allowed[PUB] == [f"{PEER4}/32"]
    assert rfp.allowed_ips_wide(False) is False and len(k.sets()) == 1


def test_a_link_raised_from_its_config_is_widened_again(k):
    """`--reassert` поднял линк из конфига — снова /32; следующий вызов расширяет."""
    rfp.allowed_ips_wide(True)
    k.allowed[PUB] = [f"{PEER4}/32"]
    assert rfp.allowed_ips_wide(True) is True and "0.0.0.0/0" in k.allowed[PUB]


def test_without_the_pair_config_nothing_is_touched(k):
    (k.awg / "awgpair.conf").unlink()
    assert rfp.allowed_ips_wide(True) is False and rfp.allowed_ips_wide(False) is False
    assert k.calls == [], "без конфига пары — вызовы awg"


def test_a_missing_interface_is_an_error_not_a_change(k):
    """Линка нет — `awg set` не проходит, и об этом говорит ошибка, а не «расширено»."""
    k.down = True
    with pytest.raises(base.RoutingError):
        rfp.allowed_ips_wide(True)


# ── рукопожатие ─────────────────────────────────────────────────────────────

@pytest.fixture()
def clock(monkeypatch):
    now = types.SimpleNamespace(t=1_800_000_000.0)
    monkeypatch.setattr(rfp, "time", types.SimpleNamespace(time=lambda: now.t))
    return now


@pytest.mark.parametrize("age, alive", [(30, True), (180, True), (181, False), (3600, False)])
def test_the_pair_link_is_alive_with_a_handshake_up_to_three_minutes_old(k, clock, age, alive):
    k.handshake[PUB] = int(clock.t) - age
    assert rfp.handshake_age() == pytest.approx(age)
    assert rfp.pair_alive() is alive


def test_no_handshake_yet_or_no_interface_is_not_alive(k, clock):
    k.handshake[PUB] = 0
    assert rfp.handshake_age() is None and rfp.pair_alive() is False, "рукопожатие «0» сочтено живым"
    k.handshake[PUB] = int(clock.t) - 10
    k.down = True
    assert rfp.handshake_age() is None and rfp.pair_alive() is False
    k.down = False
    k.handshake = {}
    assert rfp.pair_alive() is False, "пира нет — линк жив"


def test_no_awg_binary_surfaces_as_unavailable(k, clock):
    """Нет `awg` — это не «линк жив» и не «молчит», а отказ обвязки: решает вызывающий."""
    k.missing.add("awg")
    with pytest.raises(base.RoutingUnavailable):
        rfp.pair_alive()
