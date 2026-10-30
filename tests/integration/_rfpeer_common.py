"""Общее для тестов РФ-доступа через второй сервер (domain/services/reserve/rfpeer):
сервер с двумя своими слотами и парой, ядро хоста моделью (`Kernel`: правило 151,
`AllowedIPs` и рукопожатие пира линка пары, счётчики транзита), зонды слотов —
словарь `probe[слот]` (у слота «шлюзы второго» — `probe[29]`), модельные часы,
журнал перерисовок файервола — что увидела бы таблица (`collect.rfpeer()` в
момент перерисовки), журнал переключений линка активного слота.

Сервер в тестах — «Alpha» (адрес в линке пары 10.99.98.2), второй — «Beta»
(10.99.98.1). Одна и та же фикстура служит обеим ролям: транзитный — когда
вторая сторона ходит через мои шлюзы, просящий — когда я через её.
"""
from __future__ import annotations

import datetime
import subprocess
import time
import types

import pytest

from awgbot.core import config, settings
from awgbot.domain.services.reserve.rfpeer import RF_PEER_ID
from awgbot.infra import rfacct, routing
from awgbot.infra.nftguard import collect
from awgbot.infra.routing import base
from awgbot.util import timeutil

from tests.integration import _reserve_common as rc

ADMIN = config.ADMIN_ID
PEER_PUB = "DUMMY-BETA-PUB="
SELF4, PEER4 = "10.99.98.2", "10.99.98.1"
VSLOT = RF_PEER_ID
PAIR_CONF = (f"[Interface]\nAddress = {SELF4}/30\nListenPort = 47101\nPrivateKey = DUMMY-PRIV=\n\n"
             f"[Peer]\nPublicKey = {PEER_PUB}\nPresharedKey = DUMMY-PSK=\nAllowedIPs = {PEER4}/32\n"
             "PersistentKeepalive = 25-35\n")
RULE151 = f"151:\tfrom {PEER4} iif awgpair lookup {config.ROUTING_TABLE}"


class Kernel:
    """Ядро хоста для команд транзита: `ip rule` (только правило 151 и чтение),
    `awg show/set awgpair`. Прочие команды — «нет бинаря», как на машине без
    обвязки: модель не подменяет того, что не описывает. `hs_age` — возраст
    рукопожатия пира линка пары (None — не было); `up`/`dn` — счётчики транзита
    в таблице учёта; `link_down` — интерфейса пары нет; `ip_broken` — `ip` не отвечает."""

    def __init__(self):
        self.rules: list[str] = []
        self.allowed: list[str] = [f"{PEER4}/32"]
        self.hs_age: float | None = 30
        self.up = self.dn = 0
        self.link_down = False
        self.ip_broken = False
        self.calls: list[list[str]] = []

    def run(self, args, input=None, stdout=None, stderr=None, timeout=None):
        args = list(args)
        out, rc_, err = "", 0, ""
        if args[0] == "ip" and self.ip_broken:
            raise subprocess.TimeoutExpired(args, timeout)
        if args[:3] == ["ip", "rule", "show"]:
            out = "".join(f"{r}\n" for r in self.rules)
        elif args[:3] == ["ip", "rule", "add"]:
            self.calls.append(args)
            self.rules.append(self._rule(args[3:]))
        elif args[:3] == ["ip", "rule", "del"]:
            self.calls.append(args)
            r = self._rule(args[3:])
            if r in self.rules:
                self.rules.remove(r)
            else:
                rc_ = 2
        elif args[:2] == ["awg", "show"] and self.link_down:
            rc_, err = 1, "Unable to access interface: No such device"
        elif args[:4] == ["awg", "show", "awgpair", "allowed-ips"]:
            out = f"{PEER_PUB}\t{' '.join(self.allowed)}\n"
        elif args[:4] == ["awg", "show", "awgpair", "latest-handshakes"]:
            ts = int(time.time() - self.hs_age) if self.hs_age is not None else 0
            out = f"{PEER_PUB}\t{ts}\n"
        elif args[:3] == ["awg", "set", "awgpair"]:
            self.calls.append(args)
            if self.link_down:
                rc_ = 1
            else:
                self.allowed = args[6].split(",")
        else:
            raise FileNotFoundError(args[0])
        return subprocess.CompletedProcess(args, rc_, out.encode(), err.encode())

    @staticmethod
    def _rule(spec: list[str]) -> str:
        kv = dict(zip(spec[::2], spec[1::2]))
        return f"{kv['pref']}:\tfrom {kv['from']} iif {kv['iif']} lookup {kv['lookup']}"

    @property
    def wide(self) -> bool:
        return "0.0.0.0/0" in self.allowed

    def awg_sets(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["awg", "set"]]


class Clock:
    """Модельные часы: `timeutil.now()` и всё, что от него (отметки, интервал
    переключений). `step(сек)` — вперёд."""

    def __init__(self):
        self.t = datetime.datetime(2026, 11, 2, 14, 5, tzinfo=timeutil.TZ)

    def now(self):
        return self.t

    def step(self, seconds: float) -> None:
        self.t = self.t + datetime.timedelta(seconds=seconds)


@pytest.fixture()
def rf(services, fake_awg, fake_routing, make_active_client, monkeypatch, tmp_path):
    """«Alpha» с двумя своими слотами, парой с «Beta», РФ-доступом и моделью ядра.
    `rf.k` — ядро, `rf.probe` — вердикты зондов, `rf.switched` — переключения
    линка активного, `rf.fw` — что увидела таблица при каждой перерисовке,
    `rf.clock` — часы, `rf.slot_policy` — политика слотов (поставлено/снято)."""
    s = services
    admin = make_active_client(name="Админ", tg_id=ADMIN, device_limit=0)
    pi = s.add_device(admin.id, "Pi4")
    pi2 = s.add_device(admin.id, "Pi5")
    s.db.gateway_add(pi.device_id, "awglink", 443, "10.99.99.0/30", slot_id=1)
    s.db.gateway_add(pi2.device_id, "awglink2", 8443, "10.99.99.4/30", slot_id=2)
    awg = tmp_path / "awg-rf"
    awg.mkdir()
    (awg / "awgpair.conf").write_text(PAIR_CONF, encoding="utf-8")
    monkeypatch.setattr(config, "AWG_DIR", str(awg))
    monkeypatch.setattr(config, "SERVER_NAME", "Alpha")
    rc.pair_row(s)
    k = Kernel()
    monkeypatch.setattr(base, "subprocess", types.SimpleNamespace(
        run=k.run, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(rfacct, "read", lambda: rfacct.AcctState(
        handle=7, counters={rfacct.COUNTER_RFP_UP: k.up, rfacct.COUNTER_RFP_DN: k.dn}, rules=6))
    probe = {1: "ok", 2: "ok", VSLOT: "ok"}
    monkeypatch.setattr(s, "_probe_slot", lambda g, active=False: probe[g.id])
    monkeypatch.setattr(s, "_spare_verdict", lambda g, active: probe[g.id])
    monkeypatch.setattr(s, "_rt_standby_interval", lambda: 0)
    monkeypatch.setattr(s, "_rt_window_size", lambda: 10)
    switched: list[str] = []
    monkeypatch.setattr(routing.marking, "switch_active", lambda iface: switched.append(iface))
    policy: list[tuple] = []
    part = next(m for m in routing._PARTS if "drop_slot_policy" in vars(m))
    monkeypatch.setattr(part, "drop_slot_policy", lambda slot_id, iface="": policy.append(("drop", int(slot_id))))
    monkeypatch.setattr(s, "_run_link_script", lambda mode, env=None: None)

    def pair_script(mode, env=None):
        if mode == "--rollback":                              # как reserve-pair-setup.sh: конфиг линка пары прочь
            (awg / "awgpair.conf").unlink(missing_ok=True)
        return {}
    monkeypatch.setattr(s, "_run_pair_script", pair_script)
    fw: list[dict] = []
    monkeypatch.setattr(s, "_gw_firewall_refresh", lambda strict=False: fw.append(collect.rfpeer()))
    clock = Clock()
    monkeypatch.setattr(timeutil, "now", clock.now)
    settings.set_value("app.reserve.rf_via_mine", True)
    s.k, s.probe, s.switched, s.fw, s.clock, s.slot_policy = k, probe, switched, fw, clock, policy
    return s


# ── состояние второй стороны и такт ─────────────────────────────────────────

def peer_rfp(s, *, offer: bool = True, why: str = "", use: str = "") -> None:
    """Последний `st` второй стороны о транзите — как его кладёт приём `st` на диск."""
    s.rsv_update("rsv_peer_state", rfp={"offer": offer, "why": why, "use": use})


def fresh_handshake(s) -> None:
    """Сбросить кэш рукопожатия линка пары (живёт 10 с в памяти)."""
    cache = s.__dict__.get("_rf_pair_alive")
    if isinstance(cache, dict):
        cache.clear()


def tick(s) -> list:
    """Такт задания живости слотов — как в планировщике: вердикты слотов, затем
    транзит по их итогу."""
    notes = list(s.routing_liveness_tick())
    notes += s.reserve_rfpeer_tick()
    s.clock.step(30)
    return notes


def ticks(s, n: int) -> list:
    out: list = []
    for _ in range(n):
        out += tick(s)
    return out


def settle(s) -> list:
    """Три хороших подряд у всех — рубильник включён, кандидаты готовы."""
    return ticks(s, s._RT_UP_STREAK)


def own_down(s, *slots) -> None:
    for sid in slots or (1, 2):
        s.probe[sid] = "down"


def texts(notes) -> list[str]:
    return [n.text for n in notes]


def crit(notes) -> list[str]:
    return [n.text for n in notes if n.critical]
