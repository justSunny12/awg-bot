"""
Месячный сброс и отметка месяца — одной транзакцией.

Стережём: задание monthly (крон) и monthly_catchup (догон на старте) читали
отметку last_monthly_reset до транзакции сброса — сошедшись, оба сбрасывали
месяц и оба рассылали «трафик обнулён». Теперь отметка проверяется внутри
транзакции сброса (замок записи с первой строки), второй получает None.
"""
from __future__ import annotations

import threading

import pytest

from awgbot.util import timeutil

pytestmark = pytest.mark.integration


def test_the_second_reset_of_the_same_month_is_refused(services, make_active_client):
    make_active_client(name="m", tg_id=7101, traffic_limit=10 * 2 ** 30)
    services.db.set_state("last_monthly_reset", "2020-01")
    ym = timeutil.now().strftime("%Y-%m")
    notes = services.reset_monthly_traffic(ym)
    assert notes is not None and services.db.get_state("last_monthly_reset") == ym
    assert services.reset_monthly_traffic(ym) is None


def test_two_runners_reset_the_month_once(services, make_active_client):
    make_active_client(name="n", tg_id=7102, traffic_limit=10 * 2 ** 30)
    services.db.set_state("last_monthly_reset", "2020-01")
    ym = timeutil.now().strftime("%Y-%m")
    gate, got = threading.Barrier(2), []

    def run():
        gate.wait(5)
        got.append(services.reset_monthly_traffic(ym))
    ths = [threading.Thread(target=run) for _ in range(2)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(30)
    assert sorted(x is None for x in got) == [False, True], got


def test_without_a_month_the_reset_is_unconditional(services):
    """Прямой вызов (без месяца) — как прежде: сброс без проверки отметки."""
    services.db.set_state("last_monthly_reset", "2999-01")
    assert services.reset_monthly_traffic() is not None
    assert services.db.get_state("last_monthly_reset") == timeutil.now().strftime("%Y-%m")
