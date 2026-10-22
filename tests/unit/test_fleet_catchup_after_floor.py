"""Набор обновлений после подъёма линии совместимости: сервер перешёл на
4.0.1 (линия 4.0.0), агенты шлюзов остались на 3.4.0. Кэш выпусков хранит
и выпуски агентов шлюзов ниже линии — иначе шлюз перестаёт быть «сборкой
из выпуска», выпадает из набора и догон его не трогает. Окно ожидания
переподключения шлюзов после перезапуска сервера считается и от запуска
процесса: ступень с ядром длится минуты, окно от планирования истекало бы
до того, как шлюзы вернулись бы к новому процессу.
"""
from __future__ import annotations

import datetime

import pytest

from awgbot.core import compat
from awgbot.domain.fleet import order as fleet_order
from awgbot.util import timeutil
from tests.unit import _fleet_common as _fc
from tests.unit._fleet_common import rel

fleet = _fc.fleet
pytestmark = pytest.mark.unit

OLD = {"requires": "3.4.0"}
NEW = {"requires": "4.0.0"}


def _tags(fleet) -> list[str]:
    return [r.tag for r in fleet.s.fleet_releases() or []]


def _github(fleet, monkeypatch) -> None:
    """Список с GitHub после выпуска 4.0.1: сервер уже на 4.0.1, линия 4.0.0."""
    monkeypatch.setattr(compat, "COMPAT_FLOOR", "4.0.0")
    fleet.server("4.0.1")
    fleet.releases(rel("v3.3.0", main=OLD, gw=OLD), rel("v3.4.0", main=OLD, gw=OLD),
                   rel("v4.0.0", main=NEW, gw=NEW), rel("v4.0.1", main=NEW))


# ── кэш выпусков и участие шлюза ─────────────────────────────────────────────

def test_the_release_of_a_gateway_agent_below_the_floor_stays_in_the_cache(fleet, monkeypatch):
    """Шлюз на 3.4.0 без своего выпуска в кэше — не «сборка из выпуска»: не
    участник набора, команды ему нет, и он застревает на прежней версии
    навсегда — ровно то, что случилось на хостах после 4.0.1."""
    fleet.slot(1, "3.4.0")
    _github(fleet, monkeypatch)
    tags = _tags(fleet)
    assert "v3.4.0" in tags, f"выпуск агента шлюза выпал из кэша: {tags}"
    assert "v3.3.0" not in tags, f"в кэше выпуск, на котором никто не стоит: {tags}"
    row = fleet.s.fleet_gateways()[0]
    assert row["release"] is True and row["participant"] is True, row


def test_the_agent_version_from_the_snapshot_counts_when_the_gateway_is_offline(fleet, monkeypatch):
    """Проверка обновлений идёт раз в сутки и может застать шлюз не на связи:
    его выпуск всё равно хранится — иначе после возвращения он не участник
    до следующей проверки."""
    fleet.slot(1, "3.4.0", online=False)
    _github(fleet, monkeypatch)
    assert "v3.4.0" in _tags(fleet), "выпуск агента по снимку не сохранён"
    row = fleet.s.fleet_gateways()[0]
    assert row["release"] is True and row["participant"] is False, "не на связи — не участник, но сборка из выпуска"
    fleet.online(1, "3.4.0")
    assert fleet.s.fleet_gateways()[0]["participant"] is True, "вернувшийся шлюз не участник"


def test_without_gateways_the_release_below_the_floor_drops_as_before(fleet, monkeypatch):
    _github(fleet, monkeypatch)
    tags = _tags(fleet)
    assert "v3.4.0" not in tags and "v3.3.0" not in tags, f"без шлюзов кэш хранит выпуски ниже линии: {tags}"
    assert tags == ["v4.0.0", "v4.0.1"]


def test_a_role_without_a_fleet_keeps_the_old_rule(fleet, monkeypatch):
    """У агента шлюза набора нет — метода версий агентов тоже; кэш строится
    по прежнему правилу и не падает."""
    fleet.slot(1, "3.4.0")
    monkeypatch.setattr(type(fleet.s), "fleet_gateway_versions", None)
    _github(fleet, monkeypatch)
    assert _tags(fleet) == ["v4.0.0", "v4.0.1"]


def test_fleet_gateway_versions_are_the_readable_agent_versions_of_all_slots(fleet):
    fleet.slot(1, "3.4.0")                                  # по сессии
    fleet.slot(2, "4.0.0", online=False)                    # по снимку
    fleet.slot(3, "custom-build")                           # нечитаемая — не в счёт
    assert fleet.s.fleet_gateway_versions() == {(3, 4, 0), (4, 0, 0)}


def test_no_slots_give_no_versions(fleet):
    assert fleet.s.fleet_gateway_versions() == set()


# ── догон вне хода после перехода сервера ────────────────────────────────────

def test_a_gateway_left_below_the_floor_catches_up_to_the_gateway_target_on_hello(fleet, monkeypatch):
    """Сервер обновился руками до 4.0.1, шлюз вышел на связь агентом 3.4.0:
    ему уходит команда до версии роли gateway в выпуске набора — 4.0.0
    (4.0.1 шлюзам не адресован)."""
    fleet.slot(1, "3.4.0")
    _github(fleet, monkeypatch)
    out = fleet.hello(1, "3.4.0")
    sent = fleet.sent(out)
    assert [s for s, _ in sent] == [1], f"шлюзу ниже линии команда не ушла: {out}"
    assert sent[0][1]["target"] == "v4.0.0" and sent[0][1]["why"] == "follow", sent[0][1]
    assert fleet.sent(fleet.hello(1, "3.4.0")) == [(1, sent[0][1])], "повторный hello — иная команда или ни одной"


# ── окно ожидания переподключения после перезапуска сервера ──────────────────

def _ago(minutes: int) -> datetime.datetime:
    return timeutil.now() - datetime.timedelta(minutes=minutes)


def _expecting(fleet, monkeypatch, *, expect_at_min_ago: int, started_min_ago: int) -> tuple[bool, dict]:
    """Ход ждёт слот 1, который ещё не переподключился; окно — от двух
    моментов: планирования шага и запуска этого процесса."""
    fleet.releases(rel("v3.4.0", both=True), rel("v3.5.0", both=True))
    fleet.slot(1)
    fleet.offline(1)
    monkeypatch.setattr(fleet_order, "_STARTED", _ago(started_min_ago))
    run = {"id": "a1b2c3d4", "tag": "v3.5.0", "state": "running", "steps": [["main", "v3.5.0"]],
           "expect": [1], "expect_at": timeutil.to_iso(_ago(expect_at_min_ago))}
    fleet.s.fleet_run_set(run)
    return fleet.s._fleet_expecting(run, fleet.s.fleet_gateways()), run


def test_a_fresh_process_waits_for_the_gateways_even_if_the_step_was_planned_long_ago(fleet, monkeypatch):
    """Ступень с ядром шла 20 минут: процесс только поднялся, шлюзы ещё не
    успели переподключиться — набор не должен зафиксироваться без них."""
    waiting, run = _expecting(fleet, monkeypatch, expect_at_min_ago=20, started_min_ago=0)
    assert waiting is True, "окно от планирования истекло — шлюзы признаны недоступными до их hello"
    assert fleet.s.fleet_run()["expect"] == [1], "ожидание сброшено"


def test_the_wait_ends_when_both_the_step_and_the_process_are_older_than_the_window(fleet, monkeypatch):
    """Процесс работает давно и шлюз так и не вернулся — ждать дальше нельзя,
    иначе ход висит на пропавшем шлюзе."""
    waiting, run = _expecting(fleet, monkeypatch, expect_at_min_ago=20, started_min_ago=20)
    assert waiting is False, "ход ждёт шлюз бесконечно"
    assert run["expect"] == [] and run["expect_at"] == ""
    assert fleet.s.fleet_run()["expect"] == [], "сброс ожидания не записан"


def test_a_step_planned_just_now_is_still_waited_for_by_an_old_process(fleet, monkeypatch):
    """Прежнее правило сохраняется: шаг спланирован только что — ждём, даже
    если процесс (без перезапуска) живёт давно."""
    waiting, _ = _expecting(fleet, monkeypatch, expect_at_min_ago=0, started_min_ago=20)
    assert waiting is True
    assert fleet.s.fleet_run()["expect"] == [1]


def test_a_gateway_that_came_back_is_not_waited_for(fleet, monkeypatch):
    fleet.releases(rel("v3.4.0", both=True), rel("v3.5.0", both=True))
    fleet.slot(1)
    monkeypatch.setattr(fleet_order, "_STARTED", timeutil.now())
    run = {"id": "a1b2c3d4", "tag": "v3.5.0", "state": "running", "steps": [["main", "v3.5.0"]],
           "expect": [1], "expect_at": timeutil.now_iso()}
    fleet.s.fleet_run_set(run)
    assert fleet.s._fleet_expecting(run, fleet.s.fleet_gateways()) is False, "вернувшийся шлюз всё ещё ожидается"
    assert run["expect"] == []
