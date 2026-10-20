"""Общее для тестов голосования шлюзов на стороне резерва (test_reserve_vote*,
test_reserve_rfblock, test_reserve_emergency_code): резерв в паре с «Beta»,
у «Beta» три шлюза с rsv1, пакет переезда у резерва, модельные часы.

`_Box` — состояние сценария: часы (monotonic и time модулей голосования и
демпфера), счётчики rx сторон зоны по слотам (None — интерфейса нет),
взгляд резерва на основу, журнал запусков процесса открытия. Хост не
трогается: процесс открытия, файервол и зона — подмены; сеть — нет.

Шлюзы — слоты 1, 2, 3 («NASPi», «Pi5», «Pi3»); их слова резерву — те же
вызовы, что делает слушатель гостевого канала (reserve_guest_opened /
reserve_guest_in / reserve_guest_closed).
"""
from __future__ import annotations

import types

import pytest

from awgbot.core import config, settings
from awgbot.domain.services.reserve import rfblock as rfblock_mod
from awgbot.domain.services.reserve import vote as vote_mod
from awgbot.util import livestep, rsvkeys
from tests.integration import _reserve_common as rc

ADMIN = config.ADMIN_ID
PEER_ADMIN = rc.PEER_ADMIN
NAMES = {1: "NASPi", 2: "Pi5", 3: "Pi3"}
RSV_SHARE = "ab" * 16            # доля резерва в пакете (заглушка: процесс открытия подменён)
GW_SHARE = "cd" * 16
M_GEN = 1
PKG_GEN = 3


class _Clock:
    """Модельное время модулей голосования: monotonic и time — одно число."""

    def __init__(self, t: float = 10_000.0):
        self.t = float(t)

    def monotonic(self) -> float:
        return self.t

    def time(self) -> float:
        return 1_800_000_000.0 + self.t


class _Box:
    def __init__(self, services, monkeypatch):
        self.s = services
        self.clock = _Clock()
        self.rx: dict[int, int | None] = {}
        self.launched: list[dict] = []
        self.launch_fail = ""
        ns = types.SimpleNamespace(monotonic=self.clock.monotonic, time=self.clock.time)
        monkeypatch.setattr(vote_mod, "time", ns)
        monkeypatch.setattr(rfblock_mod, "time", ns)
        monkeypatch.setattr(services, "reserve_zone_link_rx", lambda slot: self.rx.get(int(slot)))
        monkeypatch.setattr(services, "_open_payload", lambda kind: {"kind": kind})
        monkeypatch.setattr(services, "_rsvopen_launch", self._launch)
        monkeypatch.setattr(services, "reserve_directions", lambda: {"in": (True, ""), "out": (True, "")})
        self.online = True
        monkeypatch.setattr(services, "reserve_online", lambda: self.online)

    def _launch(self, payload: dict) -> None:
        from awgbot.domain.services.types import ServiceError
        if self.launch_fail:
            raise ServiceError(self.launch_fail)
        self.launched.append(dict(payload))

    # ── сценарий ────────────────────────────────────────────────────────────
    def advance(self, seconds: float) -> None:
        self.clock.t += float(seconds)

    def primary(self, sees: bool) -> None:
        """Взгляд резерва на основу: видит / «недоступна» (как после пробы линка пары)."""
        if sees:
            self.s.__dict__["_reserve_liveness"] = livestep.Liveness(state="ok")
            self.s.rsv_update("rsv_peer_state", down_since="")
        else:
            self.s.__dict__["_reserve_liveness"] = livestep.Liveness(state="down")
            self.s.rsv_update("rsv_peer_state", down_since="2026-10-21T03:10:00+03:00")

    def hello(self, slot: int, *, sees: bool = True, m_gen: int = M_GEN) -> list[tuple]:
        self.rx.setdefault(int(slot), 100)
        return self.s.reserve_guest_opened(int(slot), {"slot": int(slot), "caps": ["rsv1"], "m_gen": int(m_gen),
                                                       "seen": "" if sees else "2026-10-21T03:11:00+03:00"})

    def seen(self, slot: int, sees: bool) -> list[tuple]:
        return self.s.reserve_guest_in(int(slot), "seen", {"seen": "" if sees else "2026-10-21T03:11:00+03:00"})

    def share(self, slot: int, *, m_gen: int = M_GEN, y: str = GW_SHARE) -> list[tuple]:
        return self.s.reserve_guest_in(int(slot), "share", {"share": y, "m_gen": int(m_gen)})

    def gone(self, slot: int) -> list[tuple]:
        return self.s.reserve_guest_closed(int(slot))

    def tick(self) -> list[tuple]:
        return self.s.reserve_vote_tick()

    def eval(self) -> list[tuple]:
        return self.s.reserve_vote_eval()

    def keepalive(self, *slots: int) -> None:
        """Пришёл keepalive линка шлюза: rx стороны зоны вырос."""
        for s in slots or list(self.rx):
            self.rx[s] = int(self.rx.get(s) or 0) + 148

    @property
    def case(self) -> str:
        return str(self.s.rsv_get("rsv_view").get("case") or "")

    @property
    def voters(self) -> dict:
        return dict(self.s._voters())

    def peer_admin_opened(self, opened: bool = True) -> None:
        self.s.rsv_update("rsv_peer_admin", opened=opened)


def gw_sent(actions, kind: str) -> list[tuple]:
    """Слова шлюзам вида kind: [(слот, тело)]."""
    return [(a[1], a[3]) for a in actions or [] if a and a[0] == "gw" and a[2] == kind]


def notes(actions) -> list:
    out = []
    for a in actions or []:
        if a and a[0] == "notify":
            out += list(a[1])
    return out


def sent(actions, kind: str) -> list[dict]:
    """Сообщения основе по каналу пары вида kind."""
    return [a[2] for a in actions or [] if a and a[0] == "send" and a[1] == kind]


@pytest.fixture()
def box(services, rsv, monkeypatch):
    """Резерв в паре с «Beta» (её шлюзы — слоты 1–3 с rsv1), пакет поколения
    PKG_GEN с долей резерва поколения M_GEN, готовность включена."""
    rc.pair_row(services)
    services.rsv_update("rsv_peer_state", version="4.0.0", session=True,
                        voters=[{"slot": s, "name": n} for s, n in NAMES.items()])
    services.rsv_put("rsv_pkg_in", {"gen": PKG_GEN, "fp": "ef" * 8, "share": RSV_SHARE, "m_gen": M_GEN})
    services.reserve_set_in("idle")
    settings.set_value("app.reserve.ready", True)
    b = _Box(services, monkeypatch)
    b.primary(True)
    return b


def gateway_point(slot: int) -> int:
    return rsvkeys.gateway_x(int(slot))
