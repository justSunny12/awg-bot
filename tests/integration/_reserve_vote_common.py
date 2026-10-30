"""Общее для тестов гостевого канала и кандидата кворума на стороне резерва
(test_reserve_gwguest, test_reserve_quorum_candidate, test_reserve_emergency_code
и соседи): резерв «Alpha» (сторона линии `a`) в паре с «Beta» 4.2.0 (`quo1`),
у «Beta» шлюзы на гостевом канале, пакет переезда у резерва, модельные часы.

`_Box` — состояние сценария: часы (monotonic и time модулей гостевого канала,
открытия, окна кода и кандидата, и стенные — timeutil.now), счётчики rx сторон зоны по слотам (None —
интерфейса нет), взгляд резерва на основу по линку пары, журнал запусков
процесса открытия. Хост не трогается: процесс открытия, файервол и зона —
подмены; сеть — нет.

`Cand` — мир кворума поверх `_Box`: линия `b` второй стороны с составом
(по умолчанию b, a, b1, b2 — голосующие a, b1, b2, v = 3, t = 2), её точки и
ящики словом `qkeys`, свой взгляд — машина голосующего зонда `awgqbh` с
выставленным взглядом, взгляды шлюзов — `qview` по гостевому каналу. Окно
аварийного кода открывает и закрывает кандидат (такт `reserve_quorum_candidate_tick`).

Шлюзы — слоты 1, 2, 3; их слова резерву — те же вызовы, что делает слушатель
гостевого канала (reserve_guest_opened / reserve_guest_in / reserve_guest_closed).
"""
from __future__ import annotations

import datetime
import types

import pytest

from awgbot.core import config, settings
from awgbot.domain.services.reserve import codewin as codewin_mod
from awgbot.domain.services.reserve import gwguest as gwguest_mod
from awgbot.domain.services.reserve import opening as opening_mod
from awgbot.domain.services.reserve.quorum import candidate as cand_mod
from awgbot.domain.services.reserve.quorum import inbound as qinbound_mod
from awgbot.util import livestep, rsvkeys
from awgbot.util.quorum.voter import Voter
from tests.integration import _reserve_common as rc

ADMIN = config.ADMIN_ID
PEER_ADMIN = rc.PEER_ADMIN
PKG_GEN = 3
M_GEN = 1                                   # поколение ключа пакета (M, K)
RSV_SHARE = "ab" * 16                       # доля резерва на прямой M в пакете (заглушка: процесс открытия подменён)
PEER_VERSION = "4.2.0"
PEER_CAPS = ["pair1", "pkg2", "zone1", "guest1", "keys1", "peer1", "hook1", "seal1", "quo1"]
GW_CAPS = ["rsv1", "quo1", "rsvtg1"]

LINE = "b"
MEMBERS = ["b", "a", "b1", "b2"]
Q_GEN = 4
RSV_Y = "ab" * rsvkeys.M_LEN               # заглушки точек: процесс открытия подменён
GW1_Y = "cd" * rsvkeys.M_LEN
GW2_Y = "ef" * rsvkeys.M_LEN
QBOX = "00" * rsvkeys.BOX_LEN
T_UP = 100                                  # up_seconds по умолчанию
ASK = 60                                    # ask_seconds по умолчанию
NEED2 = 60                                  # 2 × NEED_RETRY_S
RF = 900                                    # rf_block_minutes × 60


class _Clock:
    """Модельное время модулей резерва: monotonic и time — одно число."""

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
        monkeypatch.setattr(gwguest_mod, "time", ns)
        monkeypatch.setattr(opening_mod, "time", ns)
        monkeypatch.setattr(codewin_mod, "time", ns)              # окно кода и счёт попыток — те же часы
        monkeypatch.setattr(cand_mod, "time", ns)
        monkeypatch.setattr(qinbound_mod, "time", ns)             # ask_no{busy} отмечает момент теми же часами
        # стенные часы (timeutil.now: моменты ISO и elapsed — окно по линку пары, демпферы) — те же
        from awgbot.util import timeutil
        t0, wall0 = self.clock.t, timeutil.now()
        monkeypatch.setattr(timeutil, "now", lambda: wall0 + datetime.timedelta(seconds=self.clock.t - t0))
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

    def primary(self, sees: bool, since: str = "2026-10-21T03:10:00+03:00") -> None:
        """Взгляд резерва на основу по линку пары: на связи / «нет связи» (как после пробы)."""
        if sees:
            self.s.__dict__["_reserve_liveness"] = livestep.Liveness(state="ok")
            self.s.rsv_update("rsv_peer_state", down_since="")
        else:
            self.s.__dict__["_reserve_liveness"] = livestep.Liveness(state="down")
            self.s.rsv_update("rsv_peer_state", down_since=since)

    def hello(self, slot: int, *, caps: list[str] | None = None) -> list[tuple]:
        """Шлюз открыл сессию гостевого канала: `hello{slot, caps, nonce}`."""
        self.rx.setdefault(int(slot), 100)
        return self.s.reserve_guest_opened(int(slot), {"slot": int(slot), "caps": list(GW_CAPS if caps is None else caps),
                                                       "nonce": ""})

    def gone(self, slot: int) -> list[tuple]:
        return self.s.reserve_guest_closed(int(slot))

    def tick(self) -> list[tuple]:
        """Такт гостевого канала (5 с): живость, `ping`, `state` по смене."""
        return self.s.reserve_gwguest_tick()

    def keepalive(self, *slots: int) -> None:
        """Пришёл keepalive линка шлюза: rx стороны зоны вырос."""
        for s in slots or list(self.rx):
            self.rx[s] = int(self.rx.get(s) or 0) + 148

    def alive(self, slot: int) -> bool:
        """Шлюз на связи для резерва: сессия открыта и живость не «нет связи»."""
        rec = self.s._gwv.get(int(slot)) or {}
        return bool(rec.get("online")) and rec.get("lv") is not None and rec["lv"].state != "down"

    def peer_admin_opened(self, opened: bool = True) -> None:
        self.s.rsv_update("rsv_peer_admin", opened=opened)

    def peer_line(self, *, members: list[str] | None = None, active: bool = True, why: list[str] | None = None) -> None:
        """`st.quo` второй стороны: её линия, состав, включена ли и почему нет."""
        self.s.rsv_update("rsv_peer_state", quo={"l": LINE, "g": 1, "members": list(MEMBERS if members is None else members),
                                                 "active": bool(active), "why": list(why or [])})


class Cand:
    """Кандидат о линии `b`: точки и ящики приняты, свой взгляд — `awgqbh`,
    шлюзы «Beta» (узлы b1, b2 состава) — на гостевом канале."""

    def __init__(self, b: _Box, monkeypatch=None, *, members: list[str] | None = None, hello: bool = True):
        self.b = b
        self.s = b.s
        self.members = list(MEMBERS if members is None else members)
        b.peer_line(members=self.members)
        assert self.s.reserve_quorum_side() == "a", "подготовка: резерв в тестах — сторона a"
        acts = self.s.reserve_qkeys_in({"l": LINE, "m_gen": M_GEN, "q_gen": Q_GEN, "t": 2, "x": rsvkeys.RESERVE_X,
                                        "y": RSV_Y, "qbox": QBOX, "c_gen": 1, "cy": RSV_Y, "cbox": QBOX})
        assert acts, "подготовка: точки линии b не приняты"
        self.v = Voter(LINE, "a")
        self.v.view = "see"
        self.v.epoch = 7
        self.s._qvoters[f"awgq{LINE}h"] = self.v
        if hello:
            for node in self.members:
                if node.startswith(LINE) and node != LINE:
                    b.hello(int(node[len(LINE):]))

    def own(self, w: str) -> None:
        """Свой взгляд зонда на её AWG; смена — с моментом, как пишет его голосующий (quo_views)."""
        from awgbot.util import timeutil
        if self.v.view != w:
            views = dict(self.s.rsv_get("rsv_view").get("quo_views") or {})
            views[f"awgq{LINE}h"] = {"w": w, "since": timeutil.now_iso(), "l": LINE, "target": "h"}
            self.s.rsv_update("rsv_view", quo_views=views)
        self.v.view = w

    def view(self, slot: int, w: str, since: str = "") -> list[tuple]:
        return self.s.reserve_guest_in(slot, "qview", {"l": LINE, "g": 1, "w": w, "e": 7,
                                                       "since": since or f"2026-10-30T10:0{slot}:{len(w)}0+03:00"})

    def share(self, slot: int, *, q_gen: int = Q_GEN, y: str = "") -> list[tuple]:
        return self.s.reserve_guest_in(slot, "qshare", {"l": LINE, "q_gen": q_gen, "x": rsvkeys.gateway_x(slot),
                                                        "y": y or {1: GW1_Y, 2: GW2_Y}.get(slot, GW1_Y)})

    def tick(self) -> list[tuple]:
        return self.s.reserve_quorum_candidate_tick(self.b.clock.t)

    def run(self, seconds: float, step: float = 5) -> list[tuple]:
        acts: list[tuple] = []
        for _ in range(int(seconds // step)):
            self.b.advance(step)
            acts += self.tick()
        return acts

    def stage(self) -> str:
        return str((self.s.__dict__.get("_quo_exec") or {}).get("stage") or "")


def code_window_by_decision(c: Cand) -> list[tuple]:
    """Окно кода в ходе решения: свой «не вижу» и «нет» обоих шлюзов T_up —
    «забираю», лидер молчит ask_seconds, точек нет 2 × NEED_RETRY_S."""
    c.own("uns")
    c.view(1, "uns")
    c.view(2, "uns")
    acts = c.tick() + c.run(T_UP) + c.run(ASK) + c.run(NEED2)
    assert c.s.reserve_code_window()["open"], "подготовка: окно кода в ходе решения не открылось"
    return acts


def code_window_outside(c: Cand) -> list[tuple]:
    """Окно кода вне решения: свой «не вижу», шлюзы молчат (сессий нет) — голосов нет."""
    for node in c.members:
        if node.startswith(LINE) and node != LINE:
            c.b.gone(int(node[len(LINE):]))
    c.own("uns")
    acts = c.tick()
    assert c.s.reserve_code_window()["open"], "подготовка: окно кода вне решения не открылось"
    return acts


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
    """Резерв в паре с «Beta» 4.2.0 (`quo1`), пакет поколения PKG_GEN, готовность
    включена. Ход процесса открытия (open.status) — во временном каталоге (общая
    фикстура conftest); итог аварийного кода в ответе не ждётся (codewait.VERDICT_S = 0:
    «пока не ясно» сразу) — тесты, которым нужен синхронный итог, подменяют open_status сами."""
    from awgbot.domain.services.reserve import codewait
    monkeypatch.setattr(codewait, "VERDICT_S", 0.0)
    rc.pair_row(services)
    services.rsv_update("rsv_peer_state", version=PEER_VERSION, caps=list(PEER_CAPS), session=True, has_gateway=True)
    services.rsv_put("rsv_pkg_in", {"gen": PKG_GEN, "fp": "ef" * 8, "share": RSV_SHARE, "m_gen": M_GEN})
    services.reserve_set_in("idle")
    settings.set_value("app.reserve.ready", True)
    b = _Box(services, monkeypatch)
    b.primary(True)
    return b


@pytest.fixture()
def cand(box):
    """Кандидат о линии `b` (v = 3, t = 2) поверх `box`."""
    return Cand(box)


def gateway_point(slot: int) -> int:
    return rsvkeys.gateway_x(int(slot))
