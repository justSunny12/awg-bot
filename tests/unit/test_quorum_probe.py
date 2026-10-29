"""Формат пробы и ответа кворума (util/quorum/probe.py; концепт «кворум» §3.2).

Цена ошибки: ответчик, который принимает мусор или чужой формат, засчитывает
«да», которого никто не говорил; голосующий, который принимает ответ с
незнакомой ролью или счётом вне потолков, показывает в панели шлюза ложь;
датаграмма, вылезшая за потолок, не разбирается и голос молча пропадает.
"""
from __future__ import annotations

import json

import pytest

from awgbot.util.quorum.probe import MAX_DGRAM, Info, Probe, Reply

pytestmark = pytest.mark.unit

PROBE = Probe("a", 4, "a1", 12, "see", 48213)
REPLY = Reply("a", 4, 12, "lead", 48213, 2, 3, Info(hs=37, flow=True, at="2026-11-02T14:07:10"))


def _raw(**over) -> bytes:
    d = json.loads(PROBE.encode())
    d.update(over)
    return json.dumps({k: v for k, v in d.items() if v is not ...}).encode()


def _raw_reply(**over) -> bytes:
    d = json.loads(REPLY.encode())
    d.update(over)
    return json.dumps({k: v for k, v in d.items() if v is not ...}).encode()


def test_probe_matches_the_concept_example_and_reads_back():
    assert json.loads(PROBE.encode()) == {"q": 1, "l": "a", "g": 4, "n": "a1", "e": 12, "w": "see", "s": 48213}
    assert Probe.parse(PROBE.encode()) == PROBE


@pytest.mark.parametrize("w", ["see", "sus", "uns", "new", "held"])
def test_every_view_survives_the_round_trip(w):
    p = Probe("b", 1, "b58", 0, w, 0)
    assert Probe.parse(p.encode()) == p


def test_reply_matches_the_concept_example_and_reads_back():
    d = json.loads(REPLY.encode())
    assert d == {"q": 1, "l": "a", "g": 4, "e": 12, "r": "lead", "s": 48213, "y": 2, "v": 3,
                 "i": {"hs": 37, "flow": True, "at": "2026-11-02T14:07:10"}}
    assert Reply.parse(REPLY.encode()) == REPLY


@pytest.mark.parametrize("data", [
    b"", b"null", b"[]", b"{}", b"\xff\xfe", "{\"q\":1}".encode(), b"not json",
    PROBE.encode() + b"x",                         # лишние байты после объекта
    PROBE.encode() + b" " * MAX_DGRAM,             # за потолком датаграммы
    "{\"q\":1,\"l\":\"а\"}".encode("utf-8"),       # не ASCII
])
def test_garbage_is_not_a_probe_nor_a_reply(data):
    assert Probe.parse(data) is None
    assert Reply.parse(data) is None


@pytest.mark.parametrize("over", [
    {"q": 2}, {"q": "1"}, {"q": True}, {"q": ...},
    {"l": "c"}, {"l": ...}, {"g": 0}, {"g": -1}, {"g": "4"}, {"g": True}, {"g": 2 ** 31},
    {"e": -1}, {"e": 1.5}, {"e": None}, {"s": 2 ** 31}, {"s": ...},
    {"w": "maybe"}, {"w": ...}, {"n": "b1x"}, {"n": "a59"}, {"n": ""}, {"n": ...},
])
def test_a_probe_with_a_wrong_field_is_dropped(over):
    assert Probe.parse(_raw(**over)) is None, f"проба с {over} принята"


@pytest.mark.parametrize("over", [
    {"q": 0}, {"l": "x"}, {"r": "boss"}, {"r": ...}, {"g": 0}, {"e": "12"}, {"s": -5},
    {"y": 256}, {"y": -1}, {"v": 256}, {"v": ...}, {"y": False},
])
def test_a_reply_with_a_wrong_field_is_dropped(over):
    assert Reply.parse(_raw_reply(**over)) is None, f"ответ с {over} принят"


def test_reply_roles_are_the_three_of_the_concept():
    for r in ("lead", "fenced", "follow"):
        rep = Reply("b", 2, 3, r, 1, 0, 2)
        assert Reply.parse(rep.encode()) == rep


@pytest.mark.parametrize("i,want", [
    (None, Info()), ("строка", Info()), ([1], Info()), ({}, Info()),
    ({"hs": -1, "flow": 1, "at": 5}, Info(hs=0, flow=True, at="5")),
    ({"hs": 10 ** 6 + 1}, Info(hs=0)), ({"hs": True}, Info(hs=0)), ({"hs": 10 ** 6}, Info(hs=10 ** 6)),
    ({"at": "x" * 100}, Info(at="x" * 32)),
])
def test_info_is_clamped_to_its_ceilings(i, want):
    """Справка — только в потолках: число устройств 0…10⁶, отметка ≤ 32 знаков;
    кривая справка — пустая, а не отказ всего ответа (счёт ответа важнее справки)."""
    rep = Reply.parse(_raw_reply(i=i))
    assert rep is not None and rep.i == want


def test_the_largest_reply_fits_the_datagram():
    """Ответ с наибольшими значениями всех полей и не-ASCII отметкой влезает в
    потолок и читается обратно — иначе ответчик шлёт то, что голосующий отбросит."""
    big = Reply("b", 2 ** 31 - 1, 2 ** 31 - 1, "fenced", 2 ** 31 - 1, 255, 255,
                Info(hs=10 ** 6, flow=True, at="я" * 40))
    data = big.encode()
    assert len(data) <= MAX_DGRAM
    back = Reply.parse(data)
    assert back is not None and back.i.at == "я" * 32
    assert len(Probe("b", 2 ** 31 - 1, "b58", 2 ** 31 - 1, "held", 2 ** 31 - 1).encode()) <= MAX_DGRAM


def test_info_to_dict_cuts_the_mark():
    assert Info(hs=3, flow=False, at="y" * 50).to_dict() == {"hs": 3, "flow": False, "at": "y" * 32}
