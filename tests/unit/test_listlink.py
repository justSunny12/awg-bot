"""
Грамматика ссылок на экран «весь список» (util/listlink): параметр /start
`list-<вид>[-<ref>][-n]`.

Цена ошибки: параметр /start — ввод человека и одновременно то, что бот сам
вписал в хвост «и ещё N». Ссылка, которую build собрал, а parse не узнал,
открывает у админа главную вместо списка («нажал — не то»); мусор, который
parse принял, — чужое число слота или ключ снимка, пролезший мимо строгой
грамматики. Параметр длиннее 64 знаков или вне алфавита [A-Za-z0-9_-]
Telegram в ссылку не возьмёт — хвост перестанет быть ссылкой у всех.
"""
from __future__ import annotations

import re

import pytest

from awgbot.util import listlink

pytestmark = pytest.mark.unit

# все виды этапа: живые основного бота, агента и снимок события
KINDS = ["gwnets", "gwpeers", "peernets", "fwunres", "drift", "migdrop", "sshfrom", "sshunres", "tunaddr"]
TG_PAYLOAD = re.compile(r"[A-Za-z0-9_-]{1,64}")


@pytest.mark.parametrize("new", [False, True])
@pytest.mark.parametrize("ref", [0, 1, 2, 15, listlink.REF_MAX])
@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_builds_and_parses_back(kind, ref, new):
    """Что бот вписал в хвост, то и откроется: вид, слот и метка «-n» доходят
    до экрана без потерь, параметр — в алфавите Telegram."""
    p = listlink.build(kind, ref, new=new)
    assert TG_PAYLOAD.fullmatch(p), f"параметр {p!r} Telegram в ссылку не возьмёт"
    got = listlink.parse(p)
    assert got == listlink.Link(kind=kind, ref=ref, key="", new=new), (p, got)
    assert listlink.is_list(p)


@pytest.mark.parametrize("new", [False, True])
def test_a_snapshot_key_builds_and_parses_back(new):
    p = listlink.build("snap", key="k3x9a2bq7d", new=new)
    assert p == "list-snap-k3x9a2bq7d" + ("-n" if new else ""), p
    assert listlink.parse(p) == listlink.Link(kind="snap", ref=0, key="k3x9a2bq7d", new=new)


def test_the_documented_examples_parse():
    """Примеры из описания грамматики — ровно те ссылки, что уходят в тексты."""
    assert listlink.parse("list-gwnets-2") == listlink.Link("gwnets", 2)
    assert listlink.parse("list-peernets") == listlink.Link("peernets")
    assert listlink.parse("list-drift-2-n") == listlink.Link("drift", 2, new=True)
    assert listlink.parse("list-tunaddr") == listlink.Link("tunaddr")


def test_the_longest_parameter_fits_telegram():
    """Самый длинный вид (10 букв), ключ снимка и метка — с запасом в 64 знака."""
    longest = listlink.build("abcdefghij", key="z" * 10, new=True)
    assert len(longest) <= 64 and TG_PAYLOAD.fullmatch(longest), longest
    assert listlink.parse(longest) == listlink.Link("abcdefghij", key="z" * 10, new=True)
    widest_slot = listlink.build("abcdefghij", listlink.REF_MAX, new=True)
    assert len(widest_slot) <= 64 and listlink.parse(widest_slot) is not None, widest_slot


@pytest.mark.parametrize("junk", [
    "list-", "list", "list-gwnets-x", "list-snap-ABC", "list-gwnets-99999999999", "list-gwnets-2-n-n",
    "list-gwnets-0", f"list-gwnets-{listlink.REF_MAX + 1}", "list-gwnets-12345678", "list-GWNETS",
    "list-g", "list-abcdefghijk", "list-gwnets-2-t", "list-gwnets--2", "list-gwnets-2-", " list-gwnets-2",
    "list-gwnets-2\n", "list-snap-k3x9a2bq7", "list-snap-k3x9a2bq7d1", "cl-5", "gw-2-n", "", None,
])
def test_junk_is_not_a_list_link(junk):
    """Всё, что не по грамматике, — не наша ссылка: у админа покажется главная,
    у пользователя параметр пойдёт дальше как код приглашения."""
    assert listlink.parse(junk) is None, junk
    assert not listlink.is_list(junk or "")


def test_ten_digits_are_a_key_and_up_to_seven_a_slot():
    """Десять знаков [a-z0-9] — ключ снимка, и десять цифр подряд тоже: число
    длиннее семи цифр слотом не бывает, третьего разбора нет."""
    assert listlink.parse("list-snap-1234567890") == listlink.Link("snap", 0, "1234567890", False)
    assert listlink.parse("list-gwnets-1234567") == listlink.Link("gwnets", 1234567, "", False) \
        if 1234567 <= listlink.REF_MAX else listlink.parse("list-gwnets-1234567") is None
    assert listlink.parse("list-gwnets-15-n") == listlink.Link("gwnets", 15, "", True)


def test_build_without_ref_has_no_dangling_dash():
    """Вид без слота — без «-0»: такой параметр parse отверг бы."""
    assert listlink.build("peernets") == "list-peernets"
    assert listlink.build("peernets", 0, new=True) == "list-peernets-n"
