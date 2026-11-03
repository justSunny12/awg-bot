"""Порог подозрения линка пары (`reserve.suspect_seconds`) против keepalive линка
(`client_config.keepalive_seconds`): порог ниже keepalive плюс такт — ложные
«резерв недоступен» на ровном месте. Такт держит планку, а не настройку; причина —
строкой в карточке «🛟 Резерв» и замечанием при запуске."""
from __future__ import annotations

import pytest

from awgbot.core import settings
from awgbot.domain.services.reserve import liveness as lv

pytestmark = pytest.mark.unit


def _set(monkeypatch, suspect: int, keepalive="25-35") -> None:
    monkeypatch.setattr(settings, "get_int", lambda key, default=0: suspect if key == "reserve.suspect_seconds" else default)
    monkeypatch.setattr(settings, "get", lambda key, default=None: keepalive if key.endswith("keepalive_seconds") else default)


def test_a_threshold_below_keepalive_plus_tick_is_a_problem_naming_the_floor(monkeypatch):
    _set(monkeypatch, 30)
    why = lv.pair_liveness_problem()
    assert why == "Порог подозрения резерва (30 с) меньше keepalive линка плюс такт — ложные «недоступен»; действует 40 с", why
    assert "reserve.suspect_seconds" not in why, "строка для человека — без имени ключа конфига"


@pytest.mark.parametrize("keepalive,floor", [("25-35", 40), ("25", 30), ("мусор", 40), ("", 40)])
def test_the_floor_follows_the_upper_keepalive_bound(monkeypatch, keepalive, floor):
    _set(monkeypatch, floor, keepalive)
    assert lv.pair_suspect_floor_s() == floor and lv.pair_liveness_problem() == ""
    _set(monkeypatch, floor - 1, keepalive)
    assert lv.pair_liveness_problem()


def test_the_card_shows_the_problem_line():
    from awgbot.bot.texts.reserve.section.card import card_text
    d = {"peer": {"name": "Beta", "host": "203.0.113.5"}, "peer_state": {"ready": True}, "ready": True,
         "online": True, "seen": "ok", "liveness_problem": "Порог подозрения резерва (30 с) меньше keepalive линка плюс такт"}
    text = card_text(d)
    assert "⚠️ Порог подозрения резерва (30 с) меньше keepalive линка плюс такт" in text, text
    d["liveness_problem"] = ""
    assert "Порог подозрения" not in card_text(d)


def test_preflight_warns_only_when_paired(monkeypatch):
    from awgbot.runtime import preflight
    _set(monkeypatch, 30)

    class Paired:
        def reserve_paired(self):
            return True

    class Alone:
        def reserve_paired(self):
            return False
    warns = preflight._pair_liveness_warnings(Paired())
    assert len(warns) == 1 and warns[0].startswith("порог подозрения резерва (30 с) меньше"), warns
    assert preflight._pair_liveness_warnings(Alone()) == []
    _set(monkeypatch, 40)                                       # умолчание — ровно планка
    assert preflight._pair_liveness_warnings(Paired()) == []
