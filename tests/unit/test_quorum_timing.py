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
    {"grace_s": 23},                    # < 1,4·15 + 3 = 24
    {"unfence_s": 4}, {"ask_repeat_s": 4}, {"pause_s": 4}, {"tie_clear_s": 4},   # < такта 5 с
    {"unfence_s": -1}, {"pause_s": 0},
    {"pledge_s": 0}, {"pledge_s": 59}, {"pledge_s": -60},   # pledge_minutes < 1
])
def test_each_broken_link_is_named(change):
    """Каждое нарушение связки — непустая строка (её покажут в карточке и журнале)."""
    assert replace(Timing(), **change).check() != ""


@pytest.mark.parametrize("change,named", [
    ({"up_s": 79}, "up_seconds"),
    ({"yes_window_s": 35, "up_s": 200}, "yes_window_seconds"),
    ({"rf_block_s": 100}, "rf_block_minutes"),
    ({"no_s": 29}, "no_seconds"),
    ({"down_s": -1000}, "T_down_rf"),
    ({"tie_s": 100}, "tie_minutes"),
    ({"probe_s": 4}, "probe_seconds"),
    ({"port": 0}, "порт"), ({"port": 65536}, "порт"),
    ({"grace_s": 23}, "grace_seconds"),
    ({"pledge_s": 0}, "pledge_minutes"), ({"pledge_s": 59}, "pledge_minutes"),
    ({"unfence_s": 4}, "unfence_seconds"), ({"ask_repeat_s": 4}, "ask_repeat_seconds"),
    ({"pause_s": 4}, "pause_seconds"), ({"tie_clear_s": 4}, "tie_clear_seconds"),
])
def test_each_broken_link_names_its_own_key(change, named):
    """Нарушение связки называет тот ключ, который сломан: строку читают в карточке «🛟 Резерв» и в
    журнале, и админ правит именно его в reserve.yaml. Ветка, отвечающая чужим именем (или более ранняя
    ветка, перехватившая случай), отправила бы его править не ту цифру, а кворум так и не включился бы."""
    why = replace(Timing(), **change).check()
    assert named in why, f"{change}: ждали имя {named!r}, связка ответила {why!r}"


def test_a_key_below_the_tick_is_named_with_the_tick():
    """«Меньше такта» — с самим тактом в строке: без него админу не из чего понять, до скольки поднимать."""
    why = replace(Timing(), tie_clear_s=3).check()
    assert why == "tie_clear_seconds 3 < такта 5", why


def test_the_edges_of_the_link_are_inclusive_where_the_concept_says_so():
    """up = W + T_down + 20 и W = 1,4·P + 15 — ещё сходятся (знак «≥» концепта)."""
    assert replace(Timing(), up_s=80, tie_s=180).check() == ""
    assert replace(Timing(), yes_window_s=36, up_s=100).check() == ""
    assert replace(Timing(), rf_block_s=101).check() == ""


def test_the_new_links_are_inclusive_at_their_edges():
    """grace = 1,4·P + reply, ключи «не меньше такта» = такт, pledge_minutes = 1 — ещё сходятся."""
    assert replace(Timing(), grace_s=24).check() == ""
    assert replace(Timing(), unfence_s=5, ask_repeat_s=5, pause_s=5, tie_clear_s=5).check() == ""
    assert replace(Timing(), pledge_s=60).check() == ""


def test_a_zero_pledge_is_refused_with_its_key_named():
    """`pledge_minutes: 0` — обязательство «нет» снималось бы на следующем такте: отказ с именем
    ключа (его покажут журнал и карточка), а не молчаливое выключение защиты."""
    why = Timing.from_settings({"pledge_minutes": 0}).check()
    assert "pledge_minutes" in why, why


def test_the_grace_link_follows_the_probe_interval():
    """grace ≥ 1,4·P + reply для каждого P: окно прежнего поколения не короче самой длинной пробы."""
    wrong = []
    for p in range(5, 31):
        need = Fraction(7, 5) * p + 3
        for g in range(int(need) - 2, int(need) + 3):
            w = int(Fraction(7, 5) * p + 15) + 1
            t = replace(Timing(), probe_s=p, no_s=max(45, 2 * p), yes_window_s=w, up_s=w + 40, tie_s=w + 200, grace_s=g)
            if (t.check() == "") != (g >= need):
                wrong.append(f"P={p} grace={g} (нужно ≥ {float(need)})")
    assert not wrong, ", ".join(wrong)


@pytest.mark.parametrize("p,fresh", [(5, 7), (7, 10), (10, 14), (15, 21), (21, 30), (30, 42)])
def test_fresh_grows_with_the_probe_interval(p, fresh):
    """Свежесть ответа — ⌈1,4·P⌉ от P, а не 21 навсегда: сжатая (стенд) или растянутая проба
    меняет и окно свежести."""
    assert replace(Timing(), probe_s=p).fresh_s == fresh


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


def test_settings_round_trip_with_every_field_off_its_default():
    """to_settings → from_settings возвращает те же цифры, когда КАЖДОЕ поле не умолчание:
    перепутанное соответствие ключ ↔ поле на умолчаниях не видно."""
    d = Timing()
    t = Timing(probe_s=11, no_s=47, yes_window_s=43, down_s=23, unfence_s=31, up_s=111, rf_block_s=1260,
               ask_repeat_s=33, pledge_s=420, tie_s=240, tie_clear_s=61, grace_s=121, pause_s=301, port=9001)
    keys = ("probe_s", "no_s", "yes_window_s", "down_s", "unfence_s", "up_s", "rf_block_s", "ask_repeat_s", "pledge_s",
            "tie_s", "tie_clear_s", "grace_s", "pause_s")
    assert all(getattr(t, k) != getattr(d, k) for k in keys), "подготовка: поле на умолчании"
    back = Timing.from_settings({**t.to_settings(), "port": t.port}, t.to_settings()["rf_block_minutes"])
    assert back == t, f"ключ ↔ поле перепутаны: {back}"
    assert t.check() == "", t.check()


def test_the_document_keys_are_exactly_the_settings_keys():
    """Шлюзу едут ключи `to_settings`; проверка документа шлюза знает ровно их (gwcfg/check
    `_TIMING_KEYS`): новый ключ, забытый там, агент молча отбросил бы."""
    from awgbot.domain.gwcfg.check import _TIMING_KEYS
    assert set(Timing().to_settings()) == set(_TIMING_KEYS)


@pytest.mark.parametrize("q,rf", [
    (None, None), ({}, None),
    ({"probe_seconds": "x", "no_seconds": None, "yes_window_seconds": [1], "down_seconds": {},
      "up_seconds": "1.5", "pledge_minutes": "ten", "tie_minutes": object(), "port": ""}, "мусор"),
])
def test_from_settings_with_garbage_gives_the_defaults(q, rf):
    """Мусор в `reserve.quorum` — умолчание поля, а не ноль и не исключение
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
