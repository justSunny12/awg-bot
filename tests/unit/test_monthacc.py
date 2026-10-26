"""Месячный накопитель дельт счётчиков интерфейса (util/monthacc.py).

Один и тот же накопитель считает трафик линка у агента шлюза (`gw_traffic`)
и трафик пользователей через резерв у сервера (`rsv_traffic`). Счётчики ядра
обнуляются каждым подъёмом интерфейса, поэтому копятся дельты.

Цена ошибки: рестарт линка, принятый за отрицательную дельту, — месяц
«теряет» трафик; рестарт, принятый за дельту от прошлого замера, — месяц
получает минус; смена пояса на западный в первые часы месяца, обнулившая
итог, — месячная цифра в карточке сбрасывается посреди месяца.
"""
from __future__ import annotations

import pytest

from awgbot.util import monthacc

pytestmark = pytest.mark.unit


def test_the_first_reading_counts_everything_since_the_interface_came_up():
    acc = monthacc.account(None, 1000, 300, "2026-10")
    assert (acc["month"], acc["rx"], acc["tx"], acc["last_rx"], acc["last_tx"]) == ("2026-10", 1000, 300, 1000, 300)


def test_deltas_add_up_within_a_month():
    acc = monthacc.account({}, 100, 10, "2026-10")
    acc = monthacc.account(acc, 250, 40, "2026-10")
    acc = monthacc.account(acc, 250, 40, "2026-10")          # тишина — ноль
    assert (acc["rx"], acc["tx"]) == (250, 40)


def test_a_counter_that_went_down_means_a_restart_and_the_whole_value_counts():
    acc = monthacc.account(None, 5000, 900, "2026-10")
    acc = monthacc.account(acc, 120, 30, "2026-10")           # интерфейс подняли заново
    assert (acc["rx"], acc["tx"]) == (5120, 930), "рестарт съел трафик или дал минус"
    assert (acc["last_rx"], acc["last_tx"]) == (120, 30)


def test_a_new_month_starts_the_total_over_but_keeps_the_last_reading():
    acc = monthacc.account(None, 5000, 900, "2026-10")
    acc = monthacc.account(acc, 5100, 950, "2026-11")
    assert (acc["month"], acc["rx"], acc["tx"]) == ("2026-11", 100, 50), \
        "новый месяц — только дельта после последнего замера прошлого"


def test_a_stored_month_newer_than_now_is_not_reset():
    """Пояс сменили на западный в первые часы месяца: «сейчас» оказалось в
    прошлом месяце. Итог тот же, месяц записи не откатывается."""
    acc = {"month": "2026-11", "rx": 700, "tx": 70, "last_rx": 1000, "last_tx": 100}
    acc = monthacc.account(acc, 1100, 110, "2026-10")
    assert (acc["month"], acc["rx"], acc["tx"]) == ("2026-11", 800, 80)


def test_the_state_given_in_is_not_changed_in_place():
    """Накопитель — чистая функция: сервер держит две записи (out/in) и не
    должен портить одну, считая другую."""
    before = {"month": "2026-10", "rx": 1, "tx": 2, "last_rx": 3, "last_tx": 4}
    snapshot = dict(before)
    monthacc.account(before, 10, 10, "2026-11")
    assert before == snapshot
