"""Подтверждение точки ключа кворума шлюзом (концепт «кворум» §2.2, §3.3): агент
кладёт в поле `quo` снимка поколение своей точки (`q` — `point.q_gen` раздела
`quorum`), сервер принимает поле только числом (gwsnapshot.sanitize).

Цена ошибки: агент, не сказавший поколение точки, — сервер никогда не сочтёт
ключи на месте, и кворум линии не включится; агент, сказавший поколение без
точки, — сервер включит кворум, а точки для открытия у шлюза нет; мусор в
поле, принятый как есть, — сравнение поколений по строке из скомпрометированного
шлюза.
"""
from __future__ import annotations

import json

import pytest

from awgbot.domain import gwsnapshot
from awgbot.domain.gateway.reserve.quorum import GatewayQuorumMixin

pytestmark = pytest.mark.unit


class _Agent(GatewayQuorumMixin):
    def __init__(self, store):
        self._QUO_STORE = store


def _store(path, **sec) -> None:
    base = {"l": "a", "g": 3, "members": ["a", "b", "a1"], "t": 2, "port": 8789, "probes": {"h": "DUMMY"}, "e": 1}
    base.update(sec)
    path.write_text(json.dumps(base), encoding="utf-8")


def test_the_snapshot_carries_the_generation_of_the_gateways_point(tmp_path):
    p = tmp_path / "quorum.json"
    _store(p, point={"x": 2, "y": "ab" * 16, "q_gen": 5})
    assert _Agent(p).quorum_snapshot()["q"] == 5, "поколение точки не дошло до снимка"
    _store(p, point={"x": 2, "y": "cd" * 16, "q_gen": 6})
    assert _Agent(p).quorum_snapshot()["q"] == 6, "новая точка из документа — в снимке прежнее поколение"


def test_without_a_point_the_snapshot_says_zero(tmp_path):
    """Раздел без точки (ключи линии на матрице) — `q = 0`: сервер не сочтёт
    точку доставленной."""
    p = tmp_path / "quorum.json"
    _store(p, point={})
    assert _Agent(p).quorum_snapshot()["q"] == 0
    _store(p)
    assert _Agent(p).quorum_snapshot()["q"] == 0


def test_without_a_quorum_section_there_is_no_quo_field(tmp_path):
    assert _Agent(tmp_path / "none.json").quorum_snapshot() is None


@pytest.mark.parametrize("raw, want", [(7, 7), ("7", 7), (None, 0), ("", 0), ("7; rm", 0), (-3, 0), ("x", 0), ([1], 0)],
                         ids=["int", "digits", "none", "empty", "injection", "negative", "letter", "list"])
def test_the_server_takes_the_point_generation_only_as_a_number(raw, want):
    out = gwsnapshot.sanitize({"quo": {"w": "see", "g": 1, "e": 0, "q": raw}})
    assert out["quo"]["q"] == want, f"q={raw!r} принят как {out['quo']['q']!r}"


def test_a_snapshot_of_an_older_agent_without_q_reads_as_zero():
    assert gwsnapshot.sanitize({"quo": {"w": "see", "g": 1, "e": 0}})["quo"]["q"] == 0
