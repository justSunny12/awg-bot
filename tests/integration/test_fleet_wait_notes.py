"""Набор обновлений: тихие ожидания хода — в журнал и в живое сообщение.

Инцидент: ход набора пять минут показывал «ждёт очереди» у всех шлюзов и
молчал в журнале — на деле один шлюз на связи выполнял команду вне хода
(догон до прежнего выпуска), и ход ждал её итога. Теперь каждая причина
ожидания — одной строкой в журнале при её смене (не на каждом такте), а
шлюз с командой вне хода в живом сообщении назван прямо; слот не на
связи с чужой командой ход, как и прежде, не держит.
"""
from __future__ import annotations

import logging

import pytest

from awgbot.bot.texts import fleet as ft
from awgbot.core import config
from awgbot.domain.fleet import order as fleet_order
from tests.unit import _fleet_common as _fc
from tests.unit._fleet_common import rel

fleet = _fc.fleet
pytestmark = pytest.mark.integration

LOGGER = "awgbot.fleet"
WAIT_GW = "ждёт: «Шлюз 1» выполняет команду вне хода (до v4.0.0) — ждём её итога"
WAIT_RELEASES = "ждёт: выпуски ещё не перечитаны после перезапуска"


@pytest.fixture(autouse=True)
def _fresh_notes(monkeypatch):
    """Память «что уже записано» — на процесс; тесты не должны видеть чужие ходы."""
    monkeypatch.setattr(fleet_order, "_WAIT_NOTED", {})


def _waits(caplog, needle: str) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == LOGGER and r.levelno == logging.INFO and needle in r.getMessage()]


def _stand(fleet, *, online: bool = True):
    """Сервер AWG на 4.0.0, выпуски v3.4.0/v4.0.0/v4.0.1 обеим ролям; слот 1 на
    3.4.0 с догоном до v4.0.0 в пути (команда вне хода, состояние sent);
    админ нажимает «⬆️ Обновить» до v4.0.1."""
    fleet.mp.setattr(config, "INSTALLED_VERSION", "4.0.0")
    fleet.releases(rel("v3.4.0", both=True), rel("v4.0.0", both=True), rel("v4.0.1", both=True))
    fleet.slot(1, "3.4.0", online=online)
    fleet.s.fleet_slot_set(1, {"id": "c0ffee11", "run": "", "target": "v4.0.0", "prev": "v3.4.0",
                               "state": "sent", "step": "", "error": "", "why": "catchup", "hold": False,
                               "at": fleet_order._now_iso(), "digest": "", "tries": 0})
    assert fleet.start("v4.0.1") == "", "ход не начался"


@pytest.mark.parametrize("outcome", ["failed", "done"])
def test_a_foreign_command_on_a_connected_gateway_is_noted_once_until_its_answer(fleet, caplog, outcome):
    """Ход ждёт итога чужой команды: шаг сервера не делается, причина — одной
    строкой в журнале, второй такт её не дублирует; ответ шлюза (любой)
    снимает ожидание — следующий такт делает шаг сервера, строки нет."""
    _stand(fleet)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        out = fleet.tick()
        assert out.server == "" and fleet.run()["steps"] == [], "ход шагнул, пока шлюз выполняет команду вне хода"
        assert _waits(caplog, WAIT_GW) == [f"набор: ход до v4.0.1 {WAIT_GW}"], caplog.text
        out = fleet.tick()
        assert out.server == "" and len(_waits(caplog, WAIT_GW)) == 1, "повторный такт продублировал причину"
        out = fleet.ack(1, outcome, error="сеть" if outcome == "failed" else "")
        assert out.server == "apply" and fleet.run()["steps"] == [["main", "v4.0.1"]], \
            f"после ответа {outcome} на чужую команду ход не шагнул"
        assert len(_waits(caplog, "ждёт:")) == 1, "после конца ожидания в журнале лишняя строка"


def test_the_live_message_names_the_foreign_command_instead_of_the_queue(fleet):
    """Живое сообщение: шлюз на связи с командой вне хода — «выполняет команду
    вне хода…», а не «ждёт очереди» (из-за которого ход выглядел зависшим)."""
    _stand(fleet)
    fleet.tick()
    text = ft.fleet_live(fleet.s.fleet_run_plan())
    assert "🟡 «Шлюз 1» — выполняет команду вне хода: обновляется до v4.0.0, ход ждёт её итога" in text, text
    assert "«Шлюз 1» — ждёт очереди" not in text, text


def test_the_plan_carries_the_wait_reason_and_the_server_line_shows_it_until_the_answer(fleet):
    """Пока ход ждёт чужую команду, строка сервера в живом сообщении — «ждёт:
    <причина>» (та же, что в журнале), а не «ждёт очереди»; план отдаёт
    причину в `wait`. После ответа шлюза `wait` пуст, строка сервера обычная —
    иначе снятое ожидание продолжает висеть в сообщении."""
    _stand(fleet)
    fleet.tick()
    plan = fleet.s.fleet_run_plan()
    assert "выполняет команду вне хода" in plan["wait"], plan["wait"]
    text = ft.fleet_live(plan)
    assert "🟡 Сервер AWG — ждёт: «Шлюз 1» выполняет команду вне хода (до v4.0.0)" in text, text
    assert "Сервер AWG — ждёт очереди" not in text, text
    fleet.ack(1, "done")
    plan = fleet.s.fleet_run_plan()
    assert plan["wait"] == "", f"ожидание снято ответом, а план всё ещё ждёт: {plan['wait']!r}"
    text = ft.fleet_live(plan)
    assert "ждёт:" not in text, text
    assert "Сервер AWG — ждёт очереди" not in text, text


def test_a_run_that_is_not_waiting_has_an_empty_wait_and_the_queue_line(fleet):
    """Ход не ждёт (шлюзы на связи без чужих команд, шаг сервера ещё не
    сделан): `wait` пуст и строка сервера — прежняя «ждёт очереди»."""
    fleet.mp.setattr(config, "INSTALLED_VERSION", "4.0.0")
    fleet.releases(rel("v4.0.0", both=True), rel("v4.0.1", both=True))
    fleet.slot(1, "4.0.0")
    assert fleet.start("v4.0.1") == ""
    plan = fleet.s.fleet_run_plan()
    assert plan["wait"] == "", plan["wait"]
    assert "🟡 Сервер AWG — ждёт очереди" in ft.fleet_live(plan)


def test_an_offline_gateway_with_a_foreign_command_neither_holds_the_run_nor_changes_its_line(fleet, caplog):
    """Слот не на связи с чужой командой — прежняя логика: ход его не ждёт
    (шаг сервера сразу), в живом сообщении — «обновится, когда выйдет на связь»."""
    _stand(fleet, online=False)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        out = fleet.tick()
    assert out.server == "apply", "ход ждёт команду вне хода у шлюза не на связи"
    assert _waits(caplog, "ждёт:") == [], caplog.text
    text = ft.fleet_live(fleet.s.fleet_run_plan())
    assert "🟡 «Шлюз 1» — " in text and "обновится, когда выйдет на связь" in text, text
    assert "вне хода" not in text, text


def test_an_unread_release_cache_is_noted_once_and_the_run_resumes_when_it_is_back(fleet, caplog):
    """Пустой кэш выпусков после перезапуска: ход стоит, причина — одной
    строкой; выпуски перечитаны — ход шагает, новых строк ожидания нет."""
    fleet.mp.setattr(config, "INSTALLED_VERSION", "4.0.0")
    fleet.releases(rel("v4.0.0", both=True), rel("v4.0.1", both=True))
    fleet.slot(1, "4.0.0")
    assert fleet.start("v4.0.1") == ""
    fleet.s._update_store([], {})                              # кэш выпусков пуст
    with caplog.at_level(logging.INFO, logger=LOGGER):
        out = fleet.tick()
        assert out.server == "" and fleet.run()["steps"] == []
        fleet.tick()
        assert _waits(caplog, WAIT_RELEASES) == [f"набор: ход до v4.0.1 {WAIT_RELEASES}"], caplog.text
        fleet.releases(rel("v4.0.0", both=True), rel("v4.0.1", both=True))
        out = fleet.tick()
        assert out.server == "apply", "ход не возобновился после перечитывания выпусков"
        assert len(_waits(caplog, "ждёт:")) == 1, caplog.text
