"""Общее для тестов кворума резерва на стороне основного бота
(test_reserve_quorum_*): сервер «Alpha» в паре с «Beta» (пригласивший —
сторона линии `a`), у «Alpha» три шлюза: слоты 1 и 2 с агентом `quo1`, слот 3 —
прежний (без `quo1`).

`AwgHost` — конфиг интерфейса AWG файлом во временном каталоге: настоящие
`add_peer`/`remove_peer` и запись второго адреса (infra/awg/peers, addrs)
правят его, `ip` и `apply_config` — журналом. `ProbeHost` — скрипт зондов
(infra/quoprobe) моделью: какие интерфейсы стоят, что ушло в `--up`.
`Clock` — модельные часы модулей кворума (monotonic одно число).
`enable` — линия включена путём этапа ключей (шаги `_reserve_quorum`): состав
подтверждён вторым сервером, ключи кворума им приняты, снимки шлюзов состава
несут поколение точки.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from awgbot.core import config
from awgbot.infra import awg, quoprobe
from awgbot.util import wgkeys
from tests.integration import _reserve_common as rc
from tests.integration import _reserve_quorum as rq

OWN_IP = "203.0.113.10"
PEER_IP = "198.51.100.7"
SRV_PRIV, SRV_PUB = wgkeys.keypair()
OBF = {"Jc": "4", "Jmin": "10", "Jmax": "50", "S1": "20", "S2": "30", "S3": "40", "S4": "50",
       "H1": "100-200", "H2": "500000100-500000900", "H3": "1000000100-1000090000", "H4": "1500000100-1999999999",
       "I1": "<b 0xc0ffee>"}
USER1_PUB = wgkeys.keypair()[1]
USER2_PUB = wgkeys.keypair()[1]
AWG0 = (f"[Interface]\nPrivateKey = {SRV_PRIV}\nAddress = 10.8.1.1/24\nListenPort = 43125\n"
        + "".join(f"{k} = {v}\n" for k, v in OBF.items())
        + f"\n[Peer]\nPublicKey = {USER1_PUB}\nAllowedIPs = 10.8.1.2/32\n"
        f"\n[Peer]\nPublicKey = {USER2_PUB}\nAllowedIPs = 10.8.1.3/32\n")


class AwgHost:
    def __init__(self, monkeypatch, tmp_path: Path, text: str = AWG0):
        self.path = tmp_path / "awg0.conf"
        self.path.write_text(text, encoding="utf-8")
        self.ip: list[list[str]] = []
        self.applied = 0
        self.backups = 0
        monkeypatch.setattr(config, "AWG_DIR", str(tmp_path))
        monkeypatch.setattr(config, "AWG_INTERFACE", "awg0")
        monkeypatch.setattr(config, "CONF_PATH", str(self.path))
        monkeypatch.setattr(awg, "read_file", lambda p: Path(p).read_text(encoding="utf-8"))
        monkeypatch.setattr(awg, "write_file", lambda p, c: Path(p).write_text(c, encoding="utf-8"))
        monkeypatch.setattr(awg, "_backup_conf", self._backup)
        monkeypatch.setattr(awg, "apply_config", self._apply)
        from awgbot.infra.awg import peers as peersmod
        monkeypatch.setattr(awg, "add_peer", peersmod.add_peer)
        monkeypatch.setattr(awg, "remove_peer", peersmod.remove_peer)
        monkeypatch.setattr(awg, "read_server_params", lambda force=False, iface=None: {
            "obfuscation": dict(OBF), "listen_port": 43125, "server_pubkey": SRV_PUB, "psk": wgkeys.psk()})

        def run(args, **kw):
            self.ip.append(list(args))
            return types.SimpleNamespace(returncode=0)
        monkeypatch.setattr(awg, "subprocess", types.SimpleNamespace(run=run, SubprocessError=Exception))

    def _backup(self, iface=None):
        self.backups += 1

    def _apply(self, iface=None):
        self.applied += 1

    @property
    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def peers(self) -> dict[str, list[str]]:
        """Открытый ключ → значения AllowedIPs."""
        header, peers = awg._split_conf(self.text)
        out = {}
        for p in peers:
            allowed = [ln.split("=", 1)[1].strip() for ln in p["lines"] if ln.strip().startswith("AllowedIPs")]
            out[p["pubkey"]] = [v.strip() for a in allowed for v in a.split(",")]
        return out

    def addresses(self) -> list[str]:
        return [ln.split("=", 1)[1].strip() for ln in self.text.split("[Peer]", 1)[0].splitlines()
                if ln.strip().startswith("Address")]


class ProbeHost:
    def __init__(self, monkeypatch):
        self.standing: dict[str, str] = {}
        self.ups: list[tuple[str, str]] = []
        self.downs: list[str] = []
        self.wipes = 0
        monkeypatch.setattr(quoprobe, "standing", lambda: sorted(self.standing))
        monkeypatch.setattr(quoprobe, "up", self._up)
        monkeypatch.setattr(quoprobe, "down", self._down)
        monkeypatch.setattr(quoprobe, "wipe", self._wipe)

    def _up(self, name, conf):
        same = self.standing.get(name) == conf
        self.standing[name] = conf
        self.ups.append((name, conf))
        return "up: same" if same else "up: ok"

    def _down(self, name):
        self.standing.pop(name, None)
        self.downs.append(name)
        return "down: ok"

    def _wipe(self):
        self.standing.clear()
        self.wipes += 1
        return "wipe: ok"


class Clock:
    def __init__(self, t: float = 50_000.0):
        self.t = float(t)

    def monotonic(self) -> float:
        return self.t

    def time(self) -> float:
        return 1_800_000_000.0 + self.t


def add_gateways(services, make_active_client, caps: dict[int, str], tg_id: int = 7001) -> None:
    cl = make_active_client(name=f"Шлюзы {tg_id}", tg_id=tg_id)
    for slot, c in caps.items():
        dev = services.add_device(cl.id, f"GW{slot}").device_id
        services.db.gateway_add(dev, "awglink" if slot == 1 else f"awglink{slot}", 443 + slot,
                                f"10.99.99.{4 * (slot - 1)}/30", slot_id=slot)
        services.db.set_state(f"gwlink_caps_{slot}", c)


@pytest.fixture()
def quo(services, rsv, make_active_client, monkeypatch, tmp_path):
    """«Alpha» (линия a) в паре с «Beta» 4.2.0 (quo1), шлюзы: 1 и 2 — quo1, 3 — прежний."""
    from awgbot.domain.services.reserve import compat
    from awgbot.domain.services.reserve.quorum import leader as leadermod
    from awgbot.domain.services.reserve.quorum import probes as probesmod
    from awgbot.domain.services.reserve.quorum import tick as tickmod
    from awgbot.domain.services.reserve.quorum import voter as votermod
    rc.pair_row(services)
    services.rsv_update("rsv_peer_state", caps=list(compat.CAPS), session=True, version="4.2.0")
    add_gateways(services, make_active_client, {1: "cfg1,rsv1,quo1", 2: "cfg1,rsv1,quo1", 3: "cfg1,rsv1"})
    monkeypatch.setattr(services, "reserve_own_ip", lambda: OWN_IP)
    monkeypatch.setattr(services, "reserve_peer_ip", lambda: PEER_IP)
    services.awg_host = AwgHost(monkeypatch, tmp_path / "awg")
    services.probe_host = ProbeHost(monkeypatch)
    clock = Clock()
    ns = types.SimpleNamespace(monotonic=clock.monotonic, time=clock.time)
    for mod in (leadermod, probesmod, tickmod, votermod):
        monkeypatch.setattr(mod, "time", ns)
    services.clock = clock
    return services


def peer_old(services) -> None:
    """Второй сервер — 4.1.x: возможностей кворума нет."""
    from awgbot.domain.services.reserve import compat
    services.rsv_update("rsv_peer_state", caps=[c for c in compat.CAPS if c != "quo1"])


def gateways_applied(services) -> None:
    """Шлюзы состава применили документ с точкой текущего многочлена — снимок слота
    в базе (как его кладёт канал) несёт поколение точки."""
    m = services.reserve_quorum_mine() or services.reserve_quorum_proposed()
    for node in (m.gateways if m is not None else ()):
        slot = int(node[1:])
        snap = {**services.gwlink_snapshot(slot), **rq.applied_snap(services)}
        services.db.set_state(services._gwlink_key(services._GWLINK_SNAP_KEY, slot), json.dumps(snap, ensure_ascii=False))


def enable(services):
    """Линия «Alpha» включена по-настоящему: qmem_ok, ключи под кворум, qkeys_ok,
    точки у шлюзов. Возвращает действующий состав."""
    if not services.reserve_keys().get("m"):
        services.reserve_keys_init()
    rq.enable(services, lambda: gateways_applied(services))
    return services.reserve_quorum_mine()
