"""Общее для тестов состава и ключей кворума на стороне основы
(test_reserve_quorum_members, test_reserve_quorum_keys): сервер AWG «Alpha» в паре
с «Beta» пригласившим (сторона линии `a`), ключи переезда первого поколения, у
«Beta» в `st` возможности 4.2.0 (`quo1`) и живая сессия.

`Line` — состояние сценария: возможности агентов по слотам (подмена
`gwlink_agent_caps`), поле `quo` снимков шлюзов (подмена `gwlink_snapshot`),
модельные часы повторов `qmem`/`qkeys` и шаги, которыми идёт настоящая жизнь:
шлюз объявил `quo1`, второй сервер подтвердил состав (`qmem_ok`) и ключи
(`qkeys_ok`), шлюзы применили документ (их снимок несёт поколение точки).

Хост не трогается: интерфейсы зондов, пиры-зонды и конфиги зондов — подмены;
ключи зондов в конфигах — настоящие X25519 (раздел `quorum` проверяет шлюз).
"""
from __future__ import annotations

import types

import pytest

from awgbot.core import config
from awgbot.domain.services.reserve import compat
from awgbot.domain.services.reserve.quorum import keys as qkeys_mod, members as qmem_mod
from awgbot.util import probenet, wgkeys
from tests.integration import _reserve_common as rc

PEER_ADMIN = rc.PEER_ADMIN
CAPS_QUO = ["cfg1", "rsv1", "quo1"]
CAPS_OLD = ["cfg1", "rsv1"]


class Clock:
    """Часы повторов: `time.monotonic` модулей состава и ключей — одно число."""

    def __init__(self) -> None:
        self.t = 50_000.0

    def monotonic(self) -> float:
        return self.t

    def time(self) -> float:
        return 1_800_000_000.0 + self.t


def probe_conf(node: str) -> str:
    """Конфиг зонда, который пропускает проверка шлюза (адреса из блока зондов)."""
    priv, _ = wgkeys.keypair()
    _, pub = wgkeys.keypair()
    return (f"[Interface]\nAddress = {probenet.node_addr('a', node)}/32\nPrivateKey = {priv}\n\n"
            f"[Peer]\nPublicKey = {pub}\nPresharedKey = {wgkeys.psk()}\n"
            f"AllowedIPs = {probenet.responder_addr('a')}/32\nEndpoint = 198.51.100.10:51820\n")


class Line:
    def __init__(self, services, monkeypatch, make_active_client):
        self.s = services
        self.mp = monkeypatch
        self.caps: dict[int, list[str]] = {}
        self.snaps: dict[int, dict] = {}
        self.clock = Clock()
        ns = types.SimpleNamespace(monotonic=self.clock.monotonic, time=self.clock.time)
        monkeypatch.setattr(qmem_mod, "time", ns)
        monkeypatch.setattr(qkeys_mod, "time", ns)
        monkeypatch.setattr(services, "gwlink_agent_caps", lambda gid: list(self.caps.get(int(gid), [])))
        monkeypatch.setattr(services, "gwlink_snapshot", lambda gid: dict(self.snaps.get(int(gid), {})))
        # зонды — область этапа зондов; здесь они не должны трогать хост
        monkeypatch.setattr(services, "reserve_probe_peers_sync", lambda m: None)
        monkeypatch.setattr(services, "reserve_probes_apply", lambda *a, **k: None)
        self._confs: dict[str, str] = {}
        monkeypatch.setattr(services, "reserve_probe_conf", self._probe_conf)
        self._client = make_active_client(name="Шлюзы", tg_id=7001)

    def _probe_conf(self, node: str, target: str) -> str:
        key = f"{node}:{target}"
        if key not in self._confs:
            self._confs[key] = probe_conf(node)
        return self._confs[key]

    # ── факты ───────────────────────────────────────────────────────────────
    def add_gw(self, slot: int, *, quo: bool = True, name: str = "") -> None:
        dev = self.s.add_device(self._client.id, name or f"GW{slot}").device_id
        self.s.db.gateway_add(dev, "awglink" if slot == 1 else f"awglink{slot}", 443 + slot,
                              f"10.99.99.{4 * (slot - 1)}/30", slot_id=slot)
        self.caps[int(slot)] = list(CAPS_QUO if quo else CAPS_OLD)

    def gw_quo(self, slot: int, on: bool) -> None:
        self.caps[int(slot)] = list(CAPS_QUO if on else CAPS_OLD)

    def remove_gw(self, slot: int) -> None:
        """Шлюз снят: строки слота нет, его сторона в составе — до следующего такта."""
        self.s.db.gateway_delete(int(slot))
        self.caps.pop(int(slot), None)
        self.snaps.pop(int(slot), None)

    def peer_quo1(self, on: bool) -> None:
        caps = [c for c in compat.CAPS if c != "quo1"] + (["quo1"] if on else [])
        self.s.rsv_update("rsv_peer_state", caps=caps)

    def advance(self, seconds: float) -> None:
        self.clock.t += float(seconds)

    # ── шаги ────────────────────────────────────────────────────────────────
    def propose(self):
        return self.s.reserve_quorum_refresh()

    def ack_members(self) -> None:
        ack_members(self.s)

    def sync_keys(self) -> list[tuple]:
        return sync_keys(self.s)

    def gens(self) -> dict:
        return gens(self.s)

    def ack_keys(self) -> None:
        """«Beta» приняла qkeys текущих поколений (qkeys_ok)."""
        ack_keys(self.s)

    def gateways_applied(self, *slots: int) -> None:
        """Шлюзы применили документ с точкой текущего многочлена: их снимок несёт её поколение."""
        for slot in slots or [int(g.id) for g in self.s.db.gateways()]:
            self.snaps[int(slot)] = applied_snap(self.s)

    def enable(self) -> list[tuple]:
        """Состав предложен и подтверждён, ключи включены (Т-К12), «Beta» и шлюзы их приняли."""
        return enable(self.s, self.gateways_applied)


# ── шаги включения линии (общие с _reserve_quorum_common) ───────────────────

def ack_members(s) -> None:
    """«Beta» подтвердила предложенный состав (qmem_ok)."""
    m = s.reserve_quorum_proposed()
    assert m is not None, "предпосылка: состав не предложен"
    s.reserve_qmem_ok_in({"l": m.l, "g": int(m.g)})


def sync_keys(s) -> list[tuple]:
    return s.reserve_quorum_keys_sync(s.reserve_quorum_proposed(), s.reserve_quorum_wanted())


def gens(s) -> dict:
    q, k = s.reserve_qkeys(), s.reserve_keys()
    return {"m_gen": int(k.get("m_gen") or 0), "q_gen": int(q.get("q_gen") or 0), "c_gen": int(q.get("c_gen") or 0)}


def ack_keys(s) -> None:
    """«Beta» приняла qkeys текущих поколений (qkeys_ok)."""
    s.reserve_qkeys_ok_in({"l": s.reserve_quorum_side(), **gens(s)})


def applied_snap(s) -> dict:
    """Снимок шлюза, применившего документ с точкой текущего многочлена."""
    return {"quo": {"w": "see", "g": 1, "e": 0, "q": gens(s)["q_gen"]}}


def enable(s, gateways_applied) -> list[tuple]:
    """Путь включения линии: состав предложен и подтверждён, ключи включены
    (Т-К12), второй сервер их принял, шлюзы применили документ
    (`gateways_applied()` — как снимки доезжают в данном сценарии)."""
    s.reserve_quorum_refresh()
    ack_members(s)
    acts = sync_keys(s)
    assert s.reserve_quorum_keys_on(), f"предпосылка: ключи кворума не включились: {s.reserve_quorum_off_reasons()}"
    ack_keys(s)
    gateways_applied()
    assert s.reserve_quorum_active(), f"предпосылка: кворум не включён: {s.reserve_quorum_off_reasons()}"
    return acts


def notes(actions) -> list:
    out = []
    for a in actions or []:
        if a and a[0] == "notify":
            out += list(a[1])
    return out


def setup_line(services, monkeypatch, make_active_client, *, peer_quo1: bool = True) -> Line:
    """Пара записана пригласившим, ключи переезда есть, «Beta» на связи с `quo1`."""
    monkeypatch.setattr(config, "SERVER_NAME", "Alpha")
    rc.pair_row(services)
    if not services.reserve_keys().get("m"):
        services.reserve_keys_init()
    services.rsv_update("rsv_peer_state", version="4.2.0", session=True)
    w = Line(services, monkeypatch, make_active_client)
    w.peer_quo1(peer_quo1)
    return w


@pytest.fixture()
def line(services, rsv, make_active_client, monkeypatch):
    """«Alpha» в паре с «Beta» (обе 4.2.0), шлюзов пока нет — их добавляет тест."""
    return setup_line(services, monkeypatch, make_active_client)


__all__ = ["Line", "line", "setup_line", "notes", "probe_conf", "PEER_ADMIN", "ack_members", "sync_keys", "gens",
           "ack_keys", "applied_snap", "enable"]
