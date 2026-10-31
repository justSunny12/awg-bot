"""
voter.py — часть пакета quorum: правило голосующего — взгляд на лидера по
ответам на пробы, обязательство «нет», известная эпоха (концепт «кворум»
§2.4, §2.8, §3.2, §3.4.3).

Проба каждые P (с джиттером, ставит вызывающий); ответа нет за `reply_s` —
пропуск: взгляд `sus`, досылка `resend_n` проб через `resend_s`; ответа нет
`no_s` подряд — `uns` (с момента последнего ответа). «Вижу» из `uns`/`new` —
два ответа подряд, из `sus` — один. Взгляд в пробе — до этой пробы.

Обязательство «нет»: отдав кандидату `qview{uns}` под эпохой e, голосующий
не скажет лидеру «вижу» под той же эпохой — пробы идут со взглядом `held`,
пока кандидат не скажет `qrel{e}` (снято) или `qtake{e}` (эпоха e + 1), но не
дольше `pledge_s`. `qpause{until}` лидера — «не вижу» кандидату не уходит до
срока. Эпоха — наибольшая из ответов пробы, `qstate`, `state{e}`.

Машина без ввода-вывода: такт отдаёт действия: `probe` (пора слать),
`see`/`uns`/`sus` (взгляд сменился — `qview` кандидату и копия лидеру),
`adopt:<e>` (ответ с эпохой больше), `unpledge` (срок обязательства вышел).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .probe import Info, Reply
from .timing import Timing


@dataclass
class Voter:
    l: str
    me: str
    timing: Timing = field(default_factory=Timing)
    view: str = "new"
    since: float = 0.0            # с какого момента взгляд
    epoch: int = 0
    fact: int = 0                 # эпоха по фактам (ответы ответчика, слова сервера) — без поднятой собой
    started: float = 0.0
    last_reply: float = 0.0       # последний ответ (monotonic), 0 — ещё не было
    last_probe: float = 0.0
    next_probe: float = 0.0
    pending: dict = field(default_factory=dict)   # s → когда отправлена
    streak: int = 0               # ответов подряд (для «вижу» из uns/new)
    resends: int = 0              # досылок в этом подозрении
    seq: int = 0
    pledge: dict | None = None    # {"e": эпоха, "at": monotonic}
    pause_until: float = 0.0
    last: Reply | None = None     # последний ответ (роль, счёт, справка)

    # ── пробы ───────────────────────────────────────────────────────────────
    def start(self, now: float) -> None:
        self.started = now
        self.next_probe = now

    def w_for_probe(self) -> str:
        """Взгляд в пробе: `held` вместо «вижу», пока держится обязательство."""
        if self.pledge is not None and self.view in ("see", "sus"):
            return "held"
        return self.view

    def probe_out(self, now: float, interval: float) -> int:
        """Проба отправлена: номер; следующая — через interval (с джиттером вызывающего)."""
        self.seq = (self.seq + 1) % (2 ** 31)
        self.pending[self.seq] = now
        self.last_probe = now
        self.next_probe = now + float(interval)
        return self.seq

    def reply_in(self, r: Reply, now: float) -> list[str]:
        """Ответ ответчика. Действия: `see` (взгляд стал «вижу»), `adopt:<e>`."""
        out: list[str] = []
        if r.l != self.l or r.s not in self.pending:
            return out
        self.pending.clear()
        self.last_reply, self.last = now, r
        self.resends = 0
        self.pause_until = 0.0                                # пауза — до срока или первого ответа
        if int(r.e) > int(self.fact):
            self.fact = int(r.e)
        if int(r.e) > int(self.epoch):
            self.epoch = int(r.e)
            out.append(f"adopt:{self.epoch}")
        if self.view in ("uns", "new"):
            self.streak += 1
            if self.streak >= 2:
                self._set("see", now)
                out.append("see")
        elif self.view == "sus":
            self._set("see", now)
            out.append("see")
        return out

    def _set(self, w: str, now: float) -> None:
        if w != self.view:
            self.view, self.since = w, now
            if w != "see":
                self.streak = 0

    # ── такт ────────────────────────────────────────────────────────────────
    def tick(self, now: float) -> list[str]:
        out: list[str] = []
        t = self.timing
        # пропуск ответа
        lost = [s for s, at in self.pending.items() if now - at >= t.reply_s]
        if lost:
            for s in lost:
                del self.pending[s]
            self.streak = 0                                    # «вижу» из uns/new — два ответа ПОДРЯД
            if self.view == "see":
                self._set("sus", now)
                out.append("sus")
                self.resends = 0
            if self.view == "sus" and self.resends < t.resend_n:
                self.resends += 1
                self.next_probe = min(self.next_probe, now + t.resend_s)
        # «не вижу»: ответа нет no_s
        base = self.last_reply or self.started
        if base and now - base >= t.no_s and self.view != "uns" and not self.paused(now):
            self._set("uns", now)
            out.append("uns")
        # обязательство — по сроку; и когда линия ушла за его эпоху (клон или новый лидер ответил
        # эпохой больше): «не скажу «да» лидеру под e» больше не о ком — переезд состоялся
        if self.pledge is not None and (now - float(self.pledge.get("at") or 0) >= t.pledge_s
                                        or int(self.epoch) > int(self.pledge.get("e") or 0)):
            self.pledge = None
            out.append("unpledge")
        if now >= self.next_probe:
            out.append("probe")
        return out

    def paused(self, now: float) -> bool:
        return now < self.pause_until

    # ── слова кандидата и лидера ─────────────────────────────────────────────
    def said_no(self, now: float) -> None:
        """`qview{uns}` ушёл кандидату под текущей эпохой — обязательство."""
        self.pledge = {"e": int(self.epoch), "at": now}

    def release(self, e: int) -> bool:
        """`qrel{e}`: обязательство под той эпохой снято; эпоха, поднятая по `qtake{e}` до
        e + 1 без факта (ответ ответчика, `qstate`, `state{e}` её не подтвердили), — назад к
        факту: «забираю» отменено, прежний лидер под e снова может набирать «да»."""
        e = int(e)
        done = False
        if self.pledge is not None and int(self.pledge.get("e", -1)) == e:
            self.pledge = None
            done = True
        if int(self.epoch) == e + 1 and int(self.fact) <= e:
            self.epoch = int(self.fact)
            done = True
        return done

    def rollback(self) -> bool:
        """Эпоха, поднятая словом кандидата (`qtake`), а не фактом, — назад к факту: вторая
        сторона сказала, что линия на прежней эпохе (`state{idle, e}`)."""
        if int(self.epoch) > int(self.fact):
            self.epoch = int(self.fact)
            return True
        return False

    def take(self, e: int) -> bool:
        """`qtake{e}`: кандидат забирает под e + 1 — эпоха вперёд (не факт: откатится по `qrel{e}`
        или слову `state{idle}`, если открытие сорвётся), обязательство снято."""
        e = int(e)
        if e + 1 > self.epoch:
            self.epoch = e + 1
        self.pledge = None
        return True

    def epoch_in(self, e: int) -> bool:
        """Эпоха из `qstate`/`state{e}` (или журнала линии) — факт; больше известной — принята."""
        if int(e) > int(self.fact):
            self.fact = int(e)
        if int(e) > int(self.epoch):
            self.epoch = int(e)
            return True
        return False

    def raise_to(self, e: int) -> None:
        """Кандидат после «забираю»: прежнему лидеру под меньшей эпохой больше не «да» — только
        для своего слова и своей пробы; фактом (основой следующего `qtake`, `state`, `qstate`) не
        становится: сорванное открытие и снятое исполнение её не отменяют."""
        if int(e) > int(self.epoch):
            self.epoch = int(e)

    def pause(self, now: float, seconds: float) -> None:
        self.pause_until = now + min(float(seconds), float(self.timing.pause_s))

    def info(self) -> Info | None:
        return self.last.i if self.last is not None else None
