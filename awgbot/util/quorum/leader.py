"""
leader.py — часть пакета quorum: правило лидера — счёт «да», лишение кворума,
возврат к лидерству (концепт «кворум» §3.4, §3.7.12).

Каждый такт (5 с): Y — голосующие, чья последняя проба пришла со взглядом
`see`, моей эпохой, действующим поколением (или прежним в окне) и не старше
окна W. Y ≥ v/2 — лидер держится; Y < v/2 с момента s дольше T_down — лишение
кворума (`fence`); «да» кандидата в счёте — признак случая 3: лидер держится
T_down_rf. Возврат (`unfence`): Y ≥ v/2 дольше U, все «да» — с моей эпохой
(взгляд `held` не считается). На старте лидер без права записи: первое
большинство «да» снимает ограждение без окна U. Проба с эпохой больше моей —
`adopt`: эпоху держит другая сторона, решает вызывающий (`jrn_adopt`).

Машина не знает ни базы, ни сети: вход — пробы и такты с монотонным временем,
выход — действия строками; остальное (ответ пробе, справка) собирает ответчик.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .members import GenWindow, half_or_more
from .probe import Info, Probe, Reply
from .timing import Timing


@dataclass
class Seen:
    w: str
    e: int
    g: int
    at: float


@dataclass
class Leader:
    l: str
    v: int
    epoch: int
    candidate: str
    timing: Timing = field(default_factory=Timing)
    gen: GenWindow = field(default_factory=GenWindow)
    voters: tuple[str, ...] = ()
    seen: dict = field(default_factory=dict)       # узел → Seen (последняя проба, любая)
    fenced: bool = True                            # на старте — без права записи
    starting: bool = True                          # первое большинство снимает ограждение без U
    low_since: float | None = None
    high_since: float | None = None
    fenced_at: float = 0.0

    # ── вход ────────────────────────────────────────────────────────────────
    def probe_in(self, p: Probe, now: float) -> str:
        """Проба голосующего. Возвращает "" / "adopt" (эпоха больше моей) / "ignore"
        (не моя линия, чужой узел)."""
        if p.l != self.l or p.n not in self.voters:
            return "ignore"
        self.seen[p.n] = Seen(p.w, int(p.e), int(p.g), now)
        if int(p.e) > int(self.epoch):
            return "adopt"
        return ""

    def set_members(self, voters, v: int, g: int, now: float) -> None:
        self.voters = tuple(voters)
        self.v = int(v)
        self.gen.bump(int(g), now, self.timing.grace_s)
        for n in list(self.seen):
            if n not in self.voters:
                del self.seen[n]

    def set_epoch(self, e: int) -> None:
        self.epoch = int(e)

    # ── счёт ────────────────────────────────────────────────────────────────
    def yes_nodes(self, now: float) -> tuple[str, ...]:
        out = []
        for n, s in self.seen.items():
            if s.w == "see" and s.e == int(self.epoch) and self.gen.accepts(s.g, now) \
                    and now - s.at <= self.timing.yes_window_s:
                out.append(n)
        return tuple(out)

    def yes_count(self, now: float) -> int:
        return len(self.yes_nodes(now))

    def role(self) -> str:
        return "fenced" if self.fenced else "lead"

    def reply_for(self, p: Probe, now: float, info: Info | None = None) -> Reply:
        return Reply(self.l, self.gen.g, int(self.epoch), self.role(), p.s, self.yes_count(now), self.v,
                     info or Info())

    # ── такт ────────────────────────────────────────────────────────────────
    def tick(self, now: float) -> str:
        """"" / "fence" / "unfence"."""
        yes = self.yes_nodes(now)
        if half_or_more(len(yes), self.v):
            self.low_since = None
            if not self.fenced:
                return ""
            if self.high_since is None:
                self.high_since = now
            if self.starting or now - self.high_since >= self.timing.unfence_s:
                self.fenced, self.starting, self.high_since = False, False, None
                return "unfence"
            return ""
        self.high_since = None
        if self.fenced:
            return ""
        if self.low_since is None:
            self.low_since = now
        limit = self.timing.down_rf_s if self.candidate in yes else self.timing.down_s
        if now - self.low_since >= limit:
            self.fenced, self.fenced_at = True, now
            return "fence"
        return ""

    def fence_now(self, now: float) -> None:
        """Ограждение извне (старт, принятие чужой эпохи): счёт заново."""
        self.fenced, self.fenced_at, self.high_since = True, now, None
