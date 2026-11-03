"""Кулдаун рассылки (30 с на одну выборку) и повтор хвоста журнала (60 с) сравнивали
отметку с нулём, когда отметки ещё нет: monotonic — время с загрузки, и на хосте,
загруженном меньше 30 с назад, первая рассылка получала «только что уже отправляли»,
а первый хвост ждал до минуты. Отметки нет — давно (livestep.mono_since)."""
from __future__ import annotations

import pytest

from awgbot.bot.handlers.admin.broadcast import targets

pytestmark = pytest.mark.unit


@pytest.fixture
def clean_marks(monkeypatch):
    monkeypatch.setattr(targets, "_last_broadcast_at", {})
    return targets._last_broadcast_at


@pytest.mark.parametrize("uptime", [0.5, 10.0, 29.0, 1e6])
def test_without_a_mark_the_first_broadcast_is_not_on_cooldown(clean_marks, uptime):
    assert not targets._on_cooldown("all", uptime)


def test_a_fresh_mark_holds_and_an_old_one_does_not(clean_marks):
    clean_marks["all"] = 5.0
    assert targets._on_cooldown("all", 10.0)
    assert not targets._on_cooldown("all", 5.0 + targets._BROADCAST_COOLDOWN_SEC)
    assert not targets._on_cooldown("other", 10.0), "кулдаун чужой выборки"


def test_a_mark_from_a_previous_boot_is_old(clean_marks):
    clean_marks["all"] = 500000.0                                 # отметка старше загрузки — в будущем
    assert not targets._on_cooldown("all", 10.0)
