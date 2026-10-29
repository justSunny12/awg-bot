"""
timing.py — часть пакета quorum: цифры времени кворума и их связка.

Умолчания — концепт «кворум» §2.8; ключи `app.reserve.quorum.*`
(`probe_seconds`, `no_seconds`, `yes_window_seconds`, `down_seconds`,
`up_seconds`, `pledge_minutes`, `tie_minutes`, `port`) и существующий
`app.reserve.rf_block_minutes`. Связка (`check`) держит главное свойство
«лидер останавливается раньше, чем кандидат начинает»: нарушена — кворум
линии не включается.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

DEFAULT_PORT = 8789


@dataclass(frozen=True)
class Timing:
    probe_s: int = 15             # интервал пробы P (± 40 %)
    reply_s: int = 3              # ответа нет за — пропуск
    resend_s: int = 5             # досылка после пропуска: ещё resend_n проб через столько
    resend_n: int = 2
    no_s: int = 45                # «не вижу» T_no: ответа нет столько
    yes_window_s: int = 40        # окно «да» у лидера W
    down_s: int = 20              # лишение кворума T_down
    unfence_s: int = 30           # возврат к лидерству U
    up_s: int = 100               # решение кандидата T_up
    rf_block_s: int = 900         # кандидат сам видит лидера T_up_rf
    ask_repeat_s: int = 30        # повтор опроса qask (± 40 %)
    fresh_s: int = 21             # ответ свежее — qview{see} по qask сразу, иначе 3 пробы
    pledge_s: int = 600           # обязательство «нет» — не дольше
    tie_s: int = 180              # ничья T_tie
    tie_clear_s: int = 60         # отбой ничьей
    grace_s: int = 120            # окно прежнего поколения
    pause_s: int = 300            # плановая пауза лидера — не дольше
    tick_s: int = 5
    port: int = DEFAULT_PORT

    @property
    def down_rf_s(self) -> int:
        """Лидер, которого видит кандидат, держится T_down_rf = T_up_rf − (T_up − T_down)."""
        return self.rf_block_s - (self.up_s - self.down_s)

    def check(self) -> str:
        """Связка цифр; пусто — сходятся, иначе — что именно нарушено."""
        need_up = self.yes_window_s + self.down_s + self.tick_s + 15
        if self.up_s < need_up:
            return f"up_seconds {self.up_s} < yes_window_seconds + down_seconds + {self.tick_s + 15} = {need_up}"
        need_w = 1.4 * self.probe_s + 10 + 5
        if self.yes_window_s < need_w:
            return f"yes_window_seconds {self.yes_window_s} < 1,4·probe_seconds + 15 = {need_w:g}"
        if self.rf_block_s <= self.up_s:
            return f"rf_block_minutes·60 = {self.rf_block_s} ≤ up_seconds {self.up_s}"
        if self.no_s < 2 * self.probe_s:
            return f"no_seconds {self.no_s} < 2·probe_seconds"
        if self.down_rf_s <= 0:
            return "T_down_rf ≤ 0"
        if self.tie_s <= self.up_s:
            return f"tie_minutes·60 = {self.tie_s} ≤ up_seconds {self.up_s}"
        if self.probe_s < 5 or self.port <= 0 or self.port > 65535:
            return "probe_seconds < 5 или порт вне 1…65535"
        return ""

    @classmethod
    def from_settings(cls, q: dict | None, rf_block_minutes: int | None = None) -> "Timing":
        """Из раздела `app.reserve.quorum` (словарь) и `rf_block_minutes`; мусор —
        умолчание поля."""
        q = dict(q or {})
        d = cls()

        def num(key: str, default: int, mult: int = 1) -> int:
            try:
                v = int(q.get(key, default))
            except (TypeError, ValueError):
                return default
            return v * mult

        rf = d.rf_block_s
        if rf_block_minutes is not None:
            try:
                rf = int(rf_block_minutes) * 60
            except (TypeError, ValueError):
                rf = d.rf_block_s
        return cls(probe_s=num("probe_seconds", d.probe_s), no_s=num("no_seconds", d.no_s),
                   yes_window_s=num("yes_window_seconds", d.yes_window_s), down_s=num("down_seconds", d.down_s),
                   up_s=num("up_seconds", d.up_s), rf_block_s=rf,
                   pledge_s=num("pledge_minutes", d.pledge_s // 60) * 60,
                   tie_s=num("tie_minutes", d.tie_s // 60) * 60, port=num("port", d.port))


def jitter(base: float, frac: float = 0.4, rnd=random.random) -> float:
    """base ± frac (равномерно): интервал пробы, повтор опроса — не по расписанию."""
    return float(base) * (1.0 - frac + 2.0 * frac * rnd())
