"""Возможности журнала данных в паре (domain/services/reserve/compat.py):
`caps()` — постоянные возможности и, если процесс умеет журнал, `pkg3` и
`jrn1`; `journal_pair` — направление идёт журналом, когда `jrn1` у обеих
сторон; старший общий формат пакета.

Цена ошибки: `jrn1`, объявленный без JSON-функций SQLite (или когда захват
событий не встал), — основа перестаёт слать пакет на каждое изменение, а
событий нет: резерв в аварию поднимает пользователей из старого снимка;
журнал «у обеих» при одной стороне 4.0.x — резерв не понимает `jev`, основа
ждёт подтверждений вечно и не передаёт бота; смешанная пара не на `pkg2` —
резерв 4.0.x не прочитает пакет.
"""
from __future__ import annotations

import pytest

from awgbot.domain.services.reserve import compat
from awgbot.util import rsvjrn
from awgbot.util.rsvjrn import triggers

pytestmark = pytest.mark.unit


def test_a_process_with_json_and_capture_announces_the_journal(monkeypatch):
    monkeypatch.setattr(triggers, "_CAPTURE_FAILED", {})
    caps = compat.caps()
    assert caps[:len(compat.CAPS)] == list(compat.CAPS), "постоянные возможности пропали"
    assert "jrn1" in caps and "pkg3" in caps, caps


def test_without_json_functions_neither_jrn1_nor_pkg3_is_announced(monkeypatch):
    """SQLite без JSON — журнал не пишется: направление идёт прежним путём."""
    monkeypatch.setattr(triggers, "_CAPTURE_FAILED", {})
    monkeypatch.setattr(rsvjrn, "json_ok", lambda conn: False)
    caps = compat.caps()
    assert "jrn1" not in caps and "pkg3" not in caps, caps
    assert caps == list(compat.CAPS)


def test_a_failed_capture_withdraws_the_journal(monkeypatch):
    """Захват не встал (SQLite отверг триггер) — JSON есть, но журнал не объявляется."""
    monkeypatch.setattr(triggers, "_CAPTURE_FAILED", {"sql": "near \"json_object\": syntax error"})
    assert "jrn1" not in compat.caps()


def test_my_side_carries_the_same_caps(monkeypatch):
    """Матрица готовности (и `st`) видят ровно то, что процесс умеет."""
    monkeypatch.setattr(triggers, "_CAPTURE_FAILED", {})
    assert "jrn1" in compat.my_side(True, True, [51820])["caps"]
    monkeypatch.setattr(rsvjrn, "json_ok", lambda conn: False)
    assert "jrn1" not in compat.my_side(True, True, [51820])["caps"]


@pytest.mark.parametrize("mine,theirs,want", [
    (["pair1", "jrn1"], ["pair1", "jrn1"], True),
    (["pair1", "jrn1"], ["pair1"], False),
    (["pair1"], ["pair1", "jrn1"], False),
    ([], [], False),
    (None, ["jrn1"], False),
])
def test_the_journal_goes_only_when_both_sides_have_jrn1(mine, theirs, want):
    assert compat.journal_pair(mine, theirs) is want


def test_a_mixed_pair_falls_back_to_pkg2():
    """Основа 4.1.0 ↔ резерв 4.0.x — общий формат `pkg2`; обе 4.1.0 — `pkg3`."""
    new = list(compat.CAPS) + list(compat.JOURNAL_CAPS)
    assert compat.pkg_format(new, list(compat.CAPS)) == "pkg2"
    assert compat.pkg_format(list(compat.CAPS), new) == "pkg2"
    assert compat.pkg_format(new, new) == "pkg3"
