"""Матрица готовности направлений пары (domain/services/reserve/compat.py):
`direction_ready(src, dst)` — «клиенты src → на dst» — и старший общий
формат пакета.

Правило вычисляется, списка совместимых версий нет: готовы обе стороны,
резерв читает формат пакета и умеет зону, гостевой канал и голосование,
у отдающего есть шлюз с `rsv1`, выпуск принимающего не младше (по X.Y.Z),
линия совместимости одна, поколение AWG одно, порты клиентов не
пересекаются, пакет у резерва свежий.

Цена ошибки: направление «готово», а резерв не прочитает пакет или поднимет
клиентов на другом поколении AWG, — в аварии клиенты переедут в никуда;
направление «не готово» без причины — админ не знает, что чинить, и резерва
у него на деле нет.
"""
from __future__ import annotations

import pytest

from awgbot.domain.services.reserve import compat

pytestmark = pytest.mark.unit


def _side(**over) -> dict:
    kw = dict(name="Alpha", version="4.0.0", caps=list(compat.CAPS), gen=1, ports=[51820], ready=True,
              has_gateway=True, floor="4.0.0")
    kw.update(over)
    return compat.side(**kw)


# ── таблица истинности: норма и по одному сломанному условию ────────────────

def test_two_equal_sides_are_ready_both_ways():
    a, b = _side(), _side(name="Beta", ports=[443])
    assert compat.direction_ready(a, b) == (True, ""), "одинаковые стороны — оба направления"
    assert compat.direction_ready(b, a) == (True, "")


# (что ломаем у src, у dst, кусок причины)
_BROKEN = [
    ("версия src не читается", dict(version="мусор"), {}, "версия"),
    ("версия dst не читается", {}, dict(version=""), "версия"),
    ("src не готов", dict(ready=False), {}, "не готов"),
    ("dst не готов", {}, dict(ready=False), "не готов быть резервом"),
    ("нет общего формата пакета", {}, dict(caps=["pair1", "zone1", "guest1", "vote1"]), "формат"),
    ("dst не умеет зону", {}, dict(caps=["pkg2", "guest1", "vote1"]), "принимать клиентов"),
    ("dst не умеет гостевой канал", {}, dict(caps=["pkg2", "zone1", "vote1"]), "принимать клиентов"),
    ("dst не умеет голосование", {}, dict(caps=["pkg2", "zone1", "guest1"]), "принимать клиентов"),
    ("у src нет шлюза rsv1", dict(has_gateway=False), {}, "шлюз"),
    ("dst младше src", dict(version="4.1.0"), dict(version="4.0.0"), "не младше"),
    ("разные линии совместимости", {}, dict(floor="5.0.0"), "линии"),
    ("разные поколения AWG", dict(gen=1), dict(gen=2), "поколения"),
    ("порт клиентов у обоих", dict(ports=[51820, 443]), dict(ports=[443]), "443"),
]


@pytest.mark.parametrize("why,src_over,dst_over,frag", _BROKEN, ids=[b[0] for b in _BROKEN])
def test_each_broken_condition_alone_closes_the_direction_and_names_why(why, src_over, dst_over, frag):
    src, dst = _side(**src_over), _side(**{"name": "Beta", "ports": [8443], **dst_over})
    ok, reason = compat.direction_ready(src, dst)
    assert ok is False, f"{why}: направление осталось готовым"
    assert frag in reason, f"{why}: причина «{reason}» не называет, что чинить"


def test_a_stale_package_closes_the_direction_last():
    """Свежесть пакета — последнее условие: при прочих готовых строка карточки
    говорит именно про устаревшие данные."""
    ok, reason = compat.direction_ready(_side(), _side(name="Beta", ports=[443]), pkg_fresh=False)
    assert (ok, "устарели" in reason) == (False, True), reason


# ── «новый принимает старого» и мелочи версий ───────────────────────────────

def test_different_versions_make_only_the_newer_one_a_reserve():
    """Разные выпуски: клиенты старого — к новому можно, обратно — нет."""
    old, new = _side(version="4.0.0"), _side(name="Beta", version="4.1.0", ports=[443])
    assert compat.direction_ready(old, new)[0] is True, "новый не принял клиентов старого"
    ok, reason = compat.direction_ready(new, old)
    assert ok is False and "не младше" in reason, reason


def test_the_hotfix_digit_does_not_split_the_pair():
    """Хотфикс (четвёртая цифра) — одна и та же линия: X.Y.Z равны — оба направления."""
    a, b = _side(version="4.0.0"), _side(name="Beta", version="4.0.0.3", ports=[443])
    assert compat.direction_ready(a, b)[0] and compat.direction_ready(b, a)[0]


def test_an_unknown_generation_does_not_block():
    """Поколение 0 — сторона его не прочитала (стенд без манифеста): не повод
    закрывать направление, проверка — только когда известны оба."""
    assert compat.direction_ready(_side(gen=0), _side(name="Beta", gen=2, ports=[443]))[0] is True


def test_a_gateway_on_one_side_opens_only_its_own_direction():
    """Шлюз с `rsv1` только у Alpha: её клиенты к Beta переехать могут
    (падение подтвердит её шлюз), клиенты Beta к Alpha — нет."""
    alpha, beta = _side(has_gateway=True), _side(name="Beta", has_gateway=False, ports=[443])
    assert compat.direction_ready(alpha, beta) == (True, "")
    ok, reason = compat.direction_ready(beta, alpha)
    assert ok is False and "шлюз" in reason, reason


# ── формат пакета ────────────────────────────────────────────────────────────

def test_the_package_format_is_the_newest_common_one_or_empty():
    assert compat.pkg_format(["pkg2", "zone1"], ["pkg2"]) == "pkg2"
    assert compat.pkg_format(["pkg2"], ["pkg1"]) == "", "общего формата нет — пусто, а не первый попавшийся"
    assert compat.pkg_format([], []) == ""


def test_side_normalises_what_came_over_the_channel():
    """`side` из полей `hello`/`st`: None и строки не роняют матрицу."""
    s = compat.side(name="Beta", version=None, caps=None, gen=None, ports=None, ready=1, has_gateway=0)
    assert s == {"name": "Beta", "version": "", "caps": [], "gen": 0, "ports": [], "ready": True, "gw": False,
                 "floor": compat.COMPAT_FLOOR}
    assert compat.direction_ready(s, _side())[0] is False, "сторона без версии оказалась готовой"
