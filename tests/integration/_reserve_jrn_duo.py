"""Общее для тестов доставки журнала данных резерва (test_reserve_jrn_ship,
test_reserve_jrn_follow_pair): основа и резерв на двух настоящих базах, канал
пары — прямой вызов разбора сообщений второй стороны.

- основа «Alpha» — сервисы теста (`services`) в паре из
  tests/integration/_reserve_move.py (запись DNS и часы автоматов подставные);
- резерв «Beta» — вторые сервисы поверх своей временной базы; хранилище
  журнала основы у него — свой каталог (`reserve_keep_store`), чтобы и сама
  основа могла хранить журнал Beta (каждый сервер — основа своей линии и
  резерв чужой);
- бот основы на резерве (гость) — третьи сервисы поверх базы, собранной
  процессом открытия по журналу (util/rsvjrn/opener), в гостевом режиме
  (`config.GUEST` на время его вызовов), ключ `K` — в его хранилище, журнал
  данных гостя — в своём каталоге.

Каждое сообщение проверяется по схеме канала (`pairproto.check`) — поля,
которых нет у сообщения, не проходят мимо. Ключи — заглушки или случайные.
"""
from __future__ import annotations

import contextlib
import types

from awgbot.core import config
from awgbot.domain.services import Services
from awgbot.domain.services.reserve import compat
from awgbot.domain.services.reserve.journal import guestj, ship
from awgbot.infra import rsvzone
from awgbot.infra.db import Database
from awgbot.util import pairproto
from awgbot.util.rsvjrn import opener
from awgbot.util.rsvjrn.segments import Store
from awgbot.util.rsvpkg import seal

from tests.integration import _reserve_move as rm

JCAPS = list(compat.CAPS) + list(compat.JOURNAL_CAPS)
_FUTURE = "2099-01-01T00:00:00+03:00"
_START = "2026-01-01T00:00:00+03:00"


def checked(kind: str, body: dict) -> dict:
    """Тело сообщения пары после проверки схемой (как его увидит вторая сторона)."""
    return pairproto.check(pairproto.PAIR_PROTO, kind, dict(body), pairproto.INVITER)


def sends(acts: list[tuple], kind: str | None = None) -> list[tuple[str, dict]]:
    return [(a[1], a[2]) for a in acts if a and a[0] == "send" and (kind is None or a[1] == kind)]


def to_guest(acts: list[tuple], kind: str | None = None) -> list[tuple[str, dict]]:
    return [(a[1], a[2]) for a in acts if a and a[0] == "guest" and (kind is None or a[1] == kind)]


class Duo:
    """Основа (a) и резерв (b) на двух базах; гость (g) — когда открыт."""

    def __init__(self, mv, tmp_path, monkeypatch):
        self.mv, self.tmp, self.mp = mv, tmp_path, monkeypatch
        self.a = mv.svc
        self.clock = mv.clock
        stub = types.SimpleNamespace(monotonic=self.clock.monotonic)
        monkeypatch.setattr(ship, "time", stub)
        monkeypatch.setattr(guestj, "time", stub)
        bdir = tmp_path / "beta"
        bdir.mkdir()
        self.bdb = Database(str(bdir / "bot.db"))
        self.bdb.init_schema()
        self.b = Services(self.bdb)
        self.b_keep = Store(bdir / "keep")
        monkeypatch.setattr(self.b, "reserve_keep_store", lambda: self.b_keep)
        self.a_keep = Store(tmp_path / "alpha-keep")
        monkeypatch.setattr(self.a, "reserve_keep_store", lambda: self.a_keep)
        self.line = self.a.db.jrn_line()
        self.g = None
        self.gdb = None
        self.b_caps = list(JCAPS)
        self.lost: list[tuple[str, dict]] = []          # что «потерялось» в канале

    def close(self) -> None:
        for d in (self.bdb, self.gdb):
            if d is not None:
                d.close()

    # ── st резерва и сессия ──
    def st_b(self, **over) -> list:
        """`st` резерва основе: его возможности и взгляд на линию основы."""
        keep = self.b.reserve_keep_view()
        if self.b.__dict__.get("_rsv_guest_jrn", {}).get("e") and keep:
            keep["guest_e"] = int(self.b.__dict__["_rsv_guest_jrn"]["e"])
        body = {"caps": list(self.b_caps), **over}
        if "jrn1" in self.b_caps:
            body["jrn"] = {"keep": keep}
        checked("st", rm.peer_st(**body))
        return rm.st_in(self.a, **body)

    def session(self, **over) -> None:
        """Новая сессия пары: основа сверяется по первому `st` резерва."""
        self.a.reserve_session_opened({})
        self.st_b(**over)

    def journal_on(self) -> None:
        """Пара с `jrn1` у обеих сторон: захват включён, первая правка, резерв принял
        снимок (с номером после неё)."""
        self.session()
        self.a.reserve_jrn_sync()
        assert self.a.db.jrn_meta()["capture"] == 1, "захват не включился при jrn1 у обеих"
        self.edit("Начальный")
        self.snapshot()

    # ── пакет ──
    def snapshot(self, *, deliver: bool = True) -> dict:
        built = self.a.reserve_pkg_build(force=True)
        assert built is not None, "снимок не собрался"
        if deliver:
            self.deliver_pkg(built)
        return built

    def deliver_pkg(self, built: dict) -> tuple[str, str]:
        body = {"gen": built["gen"], "fp": built["fp"], "i": 0, "n": 1, "blob": "00", "share": built["share"],
                "m_gen": built["m_gen"], **({"jrn": built["jrn"]} if built.get("jrn") else {})}
        checked("pkg", body)
        reply = self.b.reserve_pkg_in(built["gen"], built["fp"], bytes.fromhex(built["blob"]), built["share"],
                                      built["m_gen"], built.get("jrn"))
        if reply[0] == "pkg_ok":
            self.a.reserve_pkg_ok(built["gen"], built["fp"])
        return reply

    # ── канал ──
    def a_to_b(self, acts: list[tuple]) -> list[tuple]:
        """Сообщения основы → разбор у резерва; его действия."""
        out: list[tuple] = []
        for kind, body in sends(acts):
            out += self.b.reserve_jrn_in(kind, checked(kind, body))
        return out

    def b_to_a(self, acts: list[tuple]) -> list[tuple]:
        out: list[tuple] = []
        for kind, body in sends(acts):
            out += self.a.reserve_jrn_in(kind, checked(kind, body))
        return out

    def tick(self) -> list[tuple]:
        """Такт журнала основы → резерв → ответы основе (одна ходка)."""
        acts = self.a.reserve_jrn_tick()
        self.b_to_a(self.a_to_b(acts))
        return acts

    # ── правки основы ──
    def edit(self, name: str) -> int:
        """Профиль с устройством — несколько событий решений."""
        db = self.a.db
        cid = db.create_client(name, 3, _START, _FUTURE, f"INV-DUMMY-{name}")
        db.create_device(cid, "Телефон", f"DUMMY-pub-{name}", "DUMMY-psk", f"10.8.1.{100 + cid % 100}")
        return cid

    # ── гость ──
    @contextlib.contextmanager
    def as_guest(self):
        """Вызовы гостя — в гостевом режиме процесса (K из его хранилища, журнал гостя)."""
        old = config.GUEST
        config.GUEST = True
        try:
            yield self.g
        finally:
            config.GUEST = old

    def open_guest(self, *, button: bool) -> opener.Opened:
        """Процесс открытия у резерва: снимок основы (как в guest.pkg) + журнал из
        хранилища резерва → база гостя; гость поднят (`rsv_in` taking)."""
        run, var = self.tmp / "grun", self.tmp / "gvar"
        run.mkdir(exist_ok=True)
        var.mkdir(exist_ok=True)
        self.mp.setattr(rsvzone, "RUN_DIR", run)
        self.mp.setattr(rsvzone, "VAR_DIR", var)
        self.mp.setattr(rsvzone, "K_FILE", run / "k")
        self.mp.setattr(rsvzone, "REP_PATH", var / "rep.bin")
        (run / "k").write_text(self.a.reserve_k().hex(), encoding="utf-8")
        # пакет, который держит резерв (`reserve/guest.pkg`): база и часть jrn — под K
        from awgbot.domain.services.reserve.package import guest_pkg_path
        with open(guest_pkg_path(), "rb") as f:
            parts = seal.open_parts(f.read(), self.a.reserve_k())
        assert isinstance(parts.get("jrn"), dict), "в пакете нет части jrn — открытие пошло бы прежним путём"
        path = str(self.tmp / "guest.db")
        with open(path, "wb") as f:
            f.write(bytes.fromhex(parts["bot"]["db"]))
        res = opener.build(path, self.a.reserve_k(), parts["jrn"], self.b_keep.path, var / "jrn", button=button)
        old = config.GUEST
        config.GUEST = True
        try:
            self.gdb = Database(path)
            self.gdb.init_schema()
            self.g = Services(self.gdb)
        finally:
            config.GUEST = old
        self.b.rsv_update("rsv_in", state="taking" if button else "serving", kind="button" if button else "dead")
        return res

    def guest_to_b(self, msgs: list[tuple[str, dict]]) -> list[tuple]:
        """Слова гостя по локальному каналу → бот резерва → его действия."""
        out: list[tuple] = []
        for kind, body in msgs:
            if kind == "released":
                out += self.b.reserve_guest_released_jrn(body)
                continue
            out += self.b.reserve_guest_jrn_word(kind, body)
        return out

    def b_to_guest(self, acts: list[tuple]) -> list[tuple[str, dict]]:
        """Действия резерва «гостю» → разбор у гостя → его ответы (по локальному каналу)."""
        out: list[tuple[str, dict]] = []
        with self.as_guest():
            for kind, body in to_guest(acts):
                out += self.g.guest_jrn_in(kind, body)
        return out


__all__ = ["Duo", "JCAPS", "checked", "sends", "to_guest"]
