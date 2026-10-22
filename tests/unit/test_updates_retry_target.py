"""«🔁 Повторить» в разделе «⬆️ Обновления» — только пока цель та же, что у
неудачи (`sections/updates._retry_due`): ход до vX откатился, а доступна уже
vY — повтор пошёл бы на vY под именем «повторить», человек ждал бы vX.
Откат не прошёл — повтор нужен при любой цели: до него сервер AWG шлюзам
команд не шлёт. Исчерпанные повторы догона шлюза — повтор, если цель
догона та же, что стоит перед шлюзом сейчас.
"""
from __future__ import annotations

import pytest

from awgbot.bot.sections.updates import _retry_due

pytestmark = pytest.mark.unit


def _plan(state: str = "", run_tag: str = "v1.2.0", tag: str = "", gateways=()) -> dict:
    run = {"state": state, "tag": run_tag, "prev": "v1.1.0"} if state else {}
    return {"tag": tag, "run": run, "gateways": list(gateways)}


def _gw(final: bool, want: str, target: str) -> dict:
    return {"slot": 1, "want": want, "upd_state": {"state": "failed", "final": final, "target": target}}


def test_a_rollback_with_the_same_target_offers_the_retry():
    """Откатились от v1.2.0, цель по-прежнему v1.2.0 — повтор честен."""
    assert _retry_due(_plan("rolled_back", "v1.2.0", tag="v1.2.0")) is True
    assert _retry_due(_plan("rolled_back", "v1.2.0", tag="1.2.0")) is True, "тег без «v» — та же цель"


def test_a_rollback_with_a_newer_target_hides_the_retry():
    """Вышла v1.3.0 — «🔁 Повторить» ушла бы на неё под чужим именем: кнопки
    нет, в разделе — обычная «⬆️ Обновить до v1.3.0»."""
    assert _retry_due(_plan("rolled_back", "v1.2.0", tag="v1.3.0")) is False


def test_a_rollback_without_a_target_still_offers_the_retry():
    """Цели в плане нет (список не перечитан, сеть упала) — кнопка остаётся:
    повтор пойдёт к той же цели, что стоит в ходе."""
    assert _retry_due(_plan("rolled_back", "v1.2.0", tag="")) is True


def test_a_failed_rollback_offers_the_retry_whatever_the_target_is():
    """Откат не прошёл — сервер AWG молчит шлюзам, пока не нажмут «🔁 Повторить»:
    новая цель этого не отменяет."""
    assert _retry_due(_plan("rollback_failed", "v1.2.0", tag="v1.3.0")) is True
    assert _retry_due(_plan("rollback_failed", "v1.2.0", tag="")) is True


def test_an_exhausted_catch_up_offers_the_retry_only_for_the_same_target():
    """Три неудачи догона до v1.2.0: цель шлюза та же — повтор; уже v1.3.0 —
    команда к ней уйдёт сама по новому счёту, «повторять» нечего."""
    assert _retry_due(_plan(gateways=[_gw(True, "v1.2.0", "v1.2.0")])) is True
    assert _retry_due(_plan(gateways=[_gw(True, "", "v1.2.0")])) is True, "без want — повтор прежней цели"
    assert _retry_due(_plan(gateways=[_gw(True, "v1.3.0", "v1.2.0")])) is False


def test_nothing_failed_means_no_retry():
    """Ход закрыт удачно, догоны не исчерпаны — кнопки повтора нет."""
    assert _retry_due(_plan("done", "v1.2.0", tag="v1.3.0", gateways=[_gw(False, "v1.3.0", "v1.3.0")])) is False
    assert _retry_due({}) is False
