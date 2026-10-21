"""Сторож изоляции тестов от настоящей гостевой зоны (autouse
`_reserve_zone_paths_in_tmp` в tests/conftest.py).

Цена ошибки: на хосте, где зона жила (стенд гоняет сценарии зоны перед
набором), тесты файервола читали настоящий /run/awg-bot/rsv/state.json в
фазе relay и получали чужое правило пересылки хвоста; тесты процесса
открытия писали open.status и rep.bin мимо временного каталога.
"""
from __future__ import annotations

import json

import pytest

from awgbot.infra import nftguard, rsvzone

pytestmark = pytest.mark.unit

PATHS = ("RUN_DIR", "VAR_DIR", "STATE_DIR", "STATE_PATH", "GUEST_SOCK", "OPEN_STATUS", "REP_PATH", "K_FILE",
         "WEBHOOK_FILE", "TG_POOL_FILE", "TG_TAKE_FILE")


@pytest.mark.parametrize("name", PATHS)
def test_no_zone_path_of_a_test_points_at_the_host(name):
    path = str(getattr(rsvzone, name))
    assert not path.startswith(("/run/", "/var/lib/")), f"rsvzone.{name} = {path}: тест видит настоящую зону хоста"


def test_the_zone_state_read_by_default_is_the_test_one():
    """`state()` берёт путь умолчанием аргумента: подмена STATE_PATH одна его не меняет."""
    assert not str(rsvzone.state.__defaults__[0]).startswith("/run/")
    rsvzone.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    rsvzone.STATE_PATH.write_text(json.dumps({"phase": "relay", "port": "45877", "relay_to": "203.0.113.5:45877"}))
    assert rsvzone.state()["phase"] == "relay"
    assert nftguard.guest_zone()["relay_to"] == "203.0.113.5:45877"


def test_each_test_starts_without_a_zone():
    assert rsvzone.state() == {} and nftguard.guest_zone() == {} and rsvzone.open_status() == {}
