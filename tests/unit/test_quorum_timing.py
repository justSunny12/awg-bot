"""Цифры времени кворума и их связка (util/quorum/timing.py; концепт «кворум» §2.8).

Цена ошибки: связка, пропустившая неверные цифры, ломает главное свойство
«лидер останавливается раньше, чем кандидат начинает» — два писателя одной
линии; связка, ругающаяся на умолчания, не включит кворум ни у кого; мусор
в настройках, ставший нулём, а не умолчанием, — пробы без интервала или
мгновенное лишение кворума.
"""
from __future__ import annotations

from dataclasses import replace
from fractions import Fraction

import pytest

from awgbot.util.quorum import timing
from awgbot.util.quorum.timing import Timing

pytestmark = pytest.mark.unit


def test_defaults_are_the_concept_numbers_and_agree():
    t = Timing()
    assert (t.probe_s, t.reply_s, t.resend_s, t.resend_n, t.no_s) == (15, 3, 5, 2, 45)
    assert (t.yes_window_s, t.down_s, t.unfence_s, t.up_s, t.rf_block_s) == (40, 20, 30, 100, 900)
    assert (t.ask_repeat_s, t.fresh_s, t.pledge_s, t.tie_s, t.tie_clear_s) == (30, 21, 600, 180, 60)
    assert (t.grace_s, t.pause_s, t.tick_s, t.port) == (120, 300, 5, 8789)
    assert t.check() == "", "умолчания должны сходиться"


def test_down_rf_keeps_the_same_gap_as_the_short_timers():
    """T_down_rf = T_up_rf − (T_up − T_down) = 900 − 80 = 820: запас случая 3 тот же, 35 с."""
    t = Timing()
    assert t.down_rf_s == 820
    assert t.rf_block_s - t.yes_window_s - t.down_rf_s - t.tick_s == t.up_s - t.yes_window_s - t.down_s - t.tick_s == 35


@pytest.mark.parametrize("change", [
    {"up_s": 79},                       # 79 < 40 + 20 + 20
    {"yes_window_s": 35, "up_s": 200},  # 35 < 1,4·15 + 15 = 36
    {"rf_block_s": 100},                # T_up_rf ≤ T_up
    {"rf_block_s": 99},
    {"no_s": 29},                       # < 2·P
    {"down_s": -1000},                  # T_down_rf ≤ 0
    {"tie_s": 100},                     # T_tie ≤ T_up
    {"probe_s": 4},
    {"port": 0}, {"port": 65536},
])
def test_each_broken_link_is_named(change):
    """Каждое нарушение связки — непустая строка (её покажут в карточке и журнале)."""
    assert replace(Timing(), **change).check() != ""


def test_the_edges_of_the_link_are_inclusive_where_the_concept_says_so():
    """up = W + T_down + 20 и W = 1,4·P + 15 — ещё сходятся (знак «≥» концепта)."""
    assert replace(Timing(), up_s=80, tie_s=180).check() == ""
    assert replace(Timing(), yes_window_s=36, up_s=100).check() == ""
    assert replace(Timing(), rf_block_s=101).check() == ""


def test_the_yes_window_link_is_exact_for_every_probe_interval():
    """`yes_window_seconds ≥ 1,4 · probe_seconds + 15` — без округления вниз: окно,
    которое на долю секунды короче самой длинной пробы с досылками, роняет «да»
    живого голосующего между пробами."""
    wrong = []
    for p in range(5, 31):
        need = Fraction(7, 5) * p + 15
        for w in range(int(need) - 2, int(need) + 3):
            t = replace(Timing(), probe_s=p, no_s=max(45, 2 * p), yes_window_s=w, up_s=w + 20 + 20, tie_s=w + 200)
            if (t.check() == "") != (w >= need):
                wrong.append(f"P={p} W={w} (нужно ≥ {float(need)})")
    assert not wrong, "связка W расходится с «≥ 1,4·P + 15»: " + ", ".join(wrong)


def test_from_settings_reads_the_keys_and_minutes():
    t = Timing.from_settings({"probe_seconds": 12, "no_seconds": "50", "yes_window_seconds": 41, "down_seconds": 21,
                              "up_seconds": 110, "pledge_minutes": 5, "tie_minutes": 4, "port": 9000},
                             rf_block_minutes=20)
    assert (t.probe_s, t.no_s, t.yes_window_s, t.down_s, t.up_s) == (12, 50, 41, 21, 110)
    assert (t.pledge_s, t.tie_s, t.rf_block_s, t.port) == (300, 240, 1200, 9000)


@pytest.mark.parametrize("q,rf", [
    (None, None), ({}, None),
    ({"probe_seconds": "x", "no_seconds": None, "yes_window_seconds": [1], "down_seconds": {},
      "up_seconds": "1.5", "pledge_minutes": "ten", "tie_minutes": object(), "port": ""}, "мусор"),
])
def test_from_settings_with_garbage_gives_the_defaults(q, rf):
    """Мусор в `app.reserve.quorum` — умолчание поля, а не ноль и не исключение
    на старте бота."""
    t = Timing.from_settings(q, rf_block_minutes=rf)
    assert t == Timing(), f"мусор дал {t}"
    assert t.check() == ""


def test_jitter_stays_within_forty_percent():
    """Проба и повтор опроса — 15 с ± 40 %: 9…21 с, не по расписанию."""
    assert timing.jitter(15, rnd=lambda: 0.0) == pytest.approx(9.0)
    assert timing.jitter(15, rnd=lambda: 1.0) == pytest.approx(21.0)
    assert timing.jitter(15, rnd=lambda: 0.5) == pytest.approx(15.0)
    vals = [timing.jitter(15) for _ in range(2000)]
    assert 9.0 <= min(vals) and max(vals) <= 21.0
    assert max(vals) - min(vals) > 6, "джиттер не разбрасывает интервал"
