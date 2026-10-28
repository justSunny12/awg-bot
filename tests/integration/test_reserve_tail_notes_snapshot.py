"""Т-Ж4 (journal/tail_notes `_tail_notes`): снимок всего списка спорных правок и
его последняя строка, когда резерв прислал строками не все.

После аварии резерв шлёт в отчёте не больше DISPUTED_MAX записей, остальное —
числом. Чего нет строками, в журнале этого бота нет — оно в журнале бота на
резерве, и снимок обязан сказать именно это. Без имени бота ссылку на снимок
не построить — снимок не пишется, «и ещё N» остаётся текстом.

Цена ошибки: «полный список в журнале бота» в снимке после аварии — админ
ищет спорные правки в журнале основы, где их нет, и не проверяет профили,
которые остались чужими; снимок без ссылки на него — запись в базе, которую
никто не откроет; ссылка на «t.me/?start=…» без имени бота — битая кнопка.
"""
from __future__ import annotations

import re

import pytest

from awgbot.core import config

pytestmark = pytest.mark.integration

_LINK = re.compile(r"start=list-snap-([a-z0-9]{10})")


def _disputed(n: int) -> list[dict]:
    return [{"table": "clients", "col": "name", "tail": f"Профиль {i} здесь", "kept": f"Профиль {i} там",
             "name": f"Профиль {i}"} for i in range(n)]


def _snaps(services) -> int:
    return services.db._connection().execute(
        "SELECT COUNT(*) FROM server_state WHERE substr(key, 1, 10) = 'list_snap:'").fetchone()[0]


@pytest.fixture()
def tj4(services, monkeypatch):
    monkeypatch.setattr(services, "reserve_peer_name", lambda: "Beta")
    monkeypatch.setattr(services, "bot_username", "test_bot", raising=False)
    return services


def _note(services, variant: str, rows: int, total: int) -> str:
    notes = services._tail_notes(variant, 0, _disputed(rows), total)
    assert len(notes) == 1 and notes[0].tg_id == int(config.ADMIN_ID), notes
    return notes[0].text


def _snap_rows(services, text: str) -> list[str]:
    m = _LINK.search(text)
    assert m, f"в Т-Ж4 нет ссылки на снимок: {text[-200:]}"
    snap = services.db.list_snap_get(m.group(1), int(config.ADMIN_ID))
    assert snap is not None, "ссылка есть, снимка нет"
    return snap["rows"]


def test_after_a_crash_the_snapshot_says_the_rest_is_in_the_reserves_bot_log(tj4):
    """9 записей строками из 12 спорных: в снимке девять строк и хвост
    «… и ещё 3 — в журнале бота на «Beta»»; в уведомлении «и ещё 7» — ссылкой."""
    text = _note(tj4, "crash", 9, 12)
    assert re.search(r'… <a href="https://t\.me/test_bot\?start=list-snap-[a-z0-9]{10}[^"]*">и ещё 7</a>$', text), \
        text.split("\n")[-1]
    rows = _snap_rows(tj4, text)
    assert len(rows) == 10, len(rows)
    assert rows[-1] == "… и ещё 3 — в журнале бота на «Beta»", rows[-1]


def test_after_a_crash_with_every_row_given_the_snapshot_has_no_tail(tj4):
    """Всё прислано строками — хвоста про журнал резерва нет, снимок — весь список."""
    rows = _snap_rows(tj4, _note(tj4, "crash", 12, 12))
    assert len(rows) == 12 and not any("в журнале бота" in r for r in rows), rows[-2:]


def test_in_c9_the_snapshot_does_not_send_to_the_reserves_log(tj4):
    """С9 — правки бота на резерве, перенесённые сюда: «в журнале бота на «Beta»» тут
    неправда; недоданное — честным «в снимок не вошли»."""
    rows = _snap_rows(tj4, _note(tj4, "c9", 9, 12))
    assert not any("в журнале бота на" in r for r in rows), rows[-1]
    assert rows[-1] == "… и ещё 3 — в снимок не вошли", rows[-1]


def test_without_a_bot_name_no_snapshot_is_written_and_the_rest_is_plain_text(services, monkeypatch):
    monkeypatch.setattr(services, "reserve_peer_name", lambda: "Beta")
    monkeypatch.setattr(services, "bot_username", "", raising=False)
    text = _note(services, "crash", 9, 12)
    assert text.split("\n")[-1] == "… и ещё 7", text.split("\n")[-1]
    assert "<a " not in text and "start=" not in text, f"ссылка без имени бота: {text[-200:]}"
    assert _snaps(services) == 0, "снимок записан, а ссылки на него нет"
