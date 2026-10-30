"""
candidate.py — часть пакета quorum: правило кандидата — счёт «нет», опрос,
решение, ничья (концепт «кворум» §3.5, §3.7.5).

«Нет» за лидера — свой взгляд `uns` и `qview{uns}` шлюзов линии по живому
каналу. Сначала смотрим сами: свой `uns` и хотя бы один `uns` шлюза — «нет» =
2 — таймер T_up, если это больше половины v, и опрос `qask` всех, кто ещё не
сказал «нет» (повтор через `ask_repeat_s`; при большинстве опрос лишний, но
быстрый — счёт для карточки и ничьей). Свой взгляд `see` при «нет»
шлюзов > v/2 — случай 3: ждём T_up_rf; свой взгляд стал `uns` в tb —
забираем не раньше min(начало + T_up_rf, tb + T_up). Любое «вижу» или
молчание, роняющее «нет» до ≤ v/2, — таймер сначала и `rel` (вызывающий шлёт
`qrel` голосующим). Решение `take` — когда «нет» > v/2 держится T_up; предусловия
открытия проверяет вызывающий (`blocked`). Ничья: v чётно, «да» = «нет» =
v/2 без молчащих дольше T_tie — эпизод; ничьей нет ≥ `tie_clear_s` — отбой.

Машина без ввода-вывода: взгляды и живость каналов кладёт вызывающий, такт
отдаёт действия строками: `ask:<узлы через запятую>`, `rel`, `take`, `tie`,
`tie_over`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .members import majority
from .probe import Info
from .timing import Timing, jitter


@dataclass
class Vote:
    w: str = "new"
    since: str = ""            # с какого момента взгляд (ISO, со слов голосующего)
    e: int = 0
    at: float = 0.0            # когда пришёл (monotonic)
    alive: bool = True         # канал голосующего жив
    said_no: bool = False      # сказал «нет» под этой эпохой — держит обязательство до qrel/qtake
    info: Info = field(default_factory=Info)
    info_at: float = 0.0


@dataclass
class Candidate:
    l: str
    v: int
    me: str
    timing: Timing = field(default_factory=Timing)
    voters: tuple[str, ...] = ()                   # все голосующие, кроме меня, — шлюзы линии
    own: str = "new"
    own_since: float = 0.0
    votes: dict = field(default_factory=dict)      # узел → Vote
    no_since: float | None = None                  # большинство «нет» держится с
    rf_since: float | None = None                  # случай 3: шлюзы не видят, я вижу — с
    asked_at: float | None = None
    ask_next: float = 0.0        # следующий повтор опроса (ask_repeat_s ± 40 %)
    asked: set = field(default_factory=set)
    tie_since: float | None = None
    tie_told: bool = False
    tie_gone_since: float | None = None
    decided: bool = False                          # «забираю» отдано — до сброса ничего

    # ── вход ────────────────────────────────────────────────────────────────
    def set_members(self, voters, v: int) -> None:
        """Состав сменился: голоса снятых уходят, «решено» снимается (порог другой — решение
        заново по тому же счёту: держащееся большинство даёт `take`, упавшее — `rel`)."""
        new_voters = tuple(n for n in voters if n != self.me)
        if new_voters != self.voters or int(v) != self.v:
            self.decided = False
        self.voters = new_voters
        self.v = int(v)
        for n in list(self.votes):
            if n not in self.voters:
                del self.votes[n]

    def set_own(self, w: str, now: float) -> None:
        if w != self.own:
            self.own, self.own_since = w, now

    def view_in(self, node: str, w: str, now: float, *, since: str = "", e: int = 0,
                info: Info | None = None) -> None:
        if node not in self.voters:
            return
        vt = self.votes.get(node) or Vote()
        vt.w, vt.since, vt.e, vt.at, vt.alive = w, since, int(e), now, True
        if w == "uns":
            vt.said_no = True
        if info is not None:
            vt.info, vt.info_at = info, now
        self.votes[node] = vt

    def channel(self, node: str, alive: bool) -> None:
        vt = self.votes.get(node)
        if vt is not None:
            vt.alive = bool(alive)
        elif node in self.voters:
            self.votes[node] = Vote(alive=bool(alive))

    def pledged(self) -> list[str]:
        """Кто держит обязательство «нет» (сказал «нет» и ещё не получил qrel/qtake) при живом канале."""
        return [n for n, vt in self.votes.items() if vt.said_no and vt.alive]

    def unpledge(self, nodes) -> None:
        for n in nodes:
            vt = self.votes.get(n)
            if vt is not None:
                vt.said_no = False

    def reset(self) -> None:
        """После решения (взято, отказано предусловиями) или смены лидера — счёт заново."""
        self.no_since = self.rf_since = self.asked_at = None
        self.asked.clear()
        self.decided = False

    # ── счёт ────────────────────────────────────────────────────────────────
    def no_nodes(self) -> tuple[str, ...]:
        out = [self.me] if self.own == "uns" else []
        out += [n for n, vt in self.votes.items() if vt.w == "uns" and vt.alive]
        return tuple(out)

    def see_nodes(self) -> tuple[str, ...]:
        out = [self.me] if self.own in ("see", "held") else []
        out += [n for n, vt in self.votes.items() if vt.w in ("see", "held") and vt.alive]
        return tuple(out)

    def silent_nodes(self) -> tuple[str, ...]:
        known = set(self.no_nodes()) | set(self.see_nodes())
        return tuple(n for n in (self.me,) + self.voters if n not in known)

    def gateway_no(self) -> int:
        return len([n for n in self.no_nodes() if n != self.me])

    def is_tie(self) -> bool:
        no, see = len(self.no_nodes()), len(self.see_nodes())
        return self.v % 2 == 0 and no == see == self.v // 2

    def take_deadline(self) -> float | None:
        """Когда «забираю», если «нет» удержится; None — большинства нет."""
        if self.own == "uns" and self.no_since is not None:
            d = self.no_since + self.timing.up_s
            if self.rf_since is not None:
                d = min(d, self.rf_since + self.timing.rf_block_s)
            return d
        if self.own != "uns" and self.rf_since is not None:
            return self.rf_since + self.timing.rf_block_s
        return None

    # ── такт ────────────────────────────────────────────────────────────────
    def tick(self, now: float) -> list[str]:
        out: list[str] = []
        no = self.no_nodes()
        maj = majority(len(no), self.v)
        if maj:
            if self.own == "uns":
                if self.no_since is None:
                    self.no_since = now
            elif self.rf_since is None:
                self.rf_since = now                      # случай 3: шлюзы не видят, я вижу
            if self.own != "uns" and self.no_since is not None:
                self.no_since = None                     # свой взгляд вернулся — короткий таймер снят
            dl = self.take_deadline()
            if dl is not None and now >= dl and not self.decided:
                self.decided = True
                out.append("take")
        elif self.no_since is not None or self.rf_since is not None:
            self.no_since = self.rf_since = None
            self.decided = False
            out.append("rel")                                # и без опроса: обязательства «нет», окно кода, исполнение
            self.asked_at = None
            self.asked.clear()
        # опрос: свой «не вижу» и хотя бы одно «нет» шлюза — спросить остальных (и при
        # большинстве тоже: лишний, но быстрый — счёт для карточки и ничьей)
        if self.own == "uns" and self.gateway_no() >= 1 and not self.decided:
            pending = [n for n in self.voters if n not in no]
            if pending and (self.asked_at is None or now >= self.ask_next):
                self.asked_at = now
                self.ask_next = now + jitter(self.timing.ask_repeat_s)
                self.asked.update(pending)
                out.append("ask:" + ",".join(pending))
        # ничья
        if self.is_tie():
            self.tie_gone_since = None
            if self.tie_since is None:
                self.tie_since = now
            if not self.tie_told and now - self.tie_since >= self.timing.tie_s:
                self.tie_told = True
                out.append("tie")
        else:
            if self.tie_told:
                if self.tie_gone_since is None:
                    self.tie_gone_since = now
                if now - self.tie_gone_since >= self.timing.tie_clear_s:
                    self.tie_told, self.tie_since, self.tie_gone_since = False, None, None
                    out.append("tie_over")
            else:
                self.tie_since = None
        return out

    def blocked(self) -> list[str]:
        """Решение не прошло предусловия открытия: «забираю» снято — `rel` всем."""
        self.decided = False
        self.no_since = self.rf_since = None
        if self.asked_at is not None:
            self.asked_at = None
            self.asked.clear()
        return ["rel"]
