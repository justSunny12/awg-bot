"""Аварийный код админа основы на резерве (vote.reserve_code_in /
reserve_code_failed): только от аккаунта роли, только в Н4, три попытки в
час, код транзитом в процесс открытия — у резерва его нет ни в базе, ни в
файлах; отказ процесса «ключ не снялся» — попытка в счёт и окно снова
открыто.

Цена ошибки: код, принятый не от админа основы (или вне Н4), — переезд по
слову постороннего или при живой основе; без счёта попыток — перебор кода;
код в базе резерва — у админа резерва вместе с долей резерва ключ целиком;
окно, не открывшееся после «не подошёл», — админ основы не может ввести
верный код.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from awgbot.core import config
from awgbot.infra import rsvzone
from awgbot.util import rsvkeys
from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import ADMIN, PEER_ADMIN, RSV_SHARE, box, notes

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["box"]

CODE = rsvkeys.code_encode(bytes(range(16)))          # верный по виду код (точка-заглушка)
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def h4(box, monkeypatch):
    monkeypatch.setattr(rsvzone, "teardown", lambda: "")
    monkeypatch.setattr(rsvzone, "wipe", lambda: "")
    box.peer_admin_opened()
    box.primary(False)
    box.tick()
    assert box.case == "H4" and box.s.reserve_code_window()["open"]
    return box


def test_the_code_from_the_peer_admin_in_h4_goes_to_the_opening_process_in_transit(h4):
    res = h4.s.reserve_code_in(PEER_ADMIN, CODE.lower().replace("-", " "))
    assert res["ok"] and res["pending"], res
    assert h4.launched == [{"kind": "code", "share": [1, RSV_SHARE], "code": CODE.replace("-", "")}], h4.launched
    assert h4.s.reserve_in() == "taking" and h4.s.rsv_get("rsv_in")["kind"] == "code"
    assert not h4.s.reserve_code_window()["open"], "окно кода открыто, пока процесс открытия идёт"


def test_the_code_is_kept_nowhere_on_the_reserve(h4):
    """После ввода кода его нет ни в базе (таблица состояния целиком и файлы
    SQLite байтами), ни в каталоге данных резерва."""
    h4.s.reserve_code_in(PEER_ADMIN, CODE)
    raw = CODE.replace("-", "")
    rows = h4.s.db._connection().execute("SELECT key, value FROM server_state").fetchall()
    leaked = [r["key"] for r in rows if raw in str(r["value"]) or CODE in str(r["value"])]
    assert not leaked, f"аварийный код записан у резерва: {leaked}"
    for p in [Path(h4.s.db.path), *Path(h4.s.db.path).parent.glob(Path(h4.s.db.path).name + "-*"),
              *[f for f in Path(config.DATA_DIR).rglob("*") if f.is_file()]]:
        if p.exists() and p.stat().st_size < 8_000_000:
            body = p.read_bytes()
            assert raw.encode() not in body and CODE.encode() not in body, f"код в файле резерва {p}"


@pytest.mark.parametrize("uid", [ADMIN, PEER_ADMIN + 1], ids=["reserve_admin", "stranger"])
def test_a_code_not_from_the_peer_admin_is_refused(h4, uid):
    res = h4.s.reserve_code_in(uid, CODE)
    assert not res["ok"] and res["why"] == "closed" and not h4.launched, f"код принят от {uid}"
    assert h4.s.reserve_code_window()["open"], "чужая попытка закрыла окно"


def test_a_code_outside_h4_is_refused(box):
    box.primary(False)
    box.hello(1, sees=True)                             # случай 2
    res = box.s.reserve_code_in(PEER_ADMIN, CODE)
    assert not res["ok"] and res["why"] == "closed" and not box.launched


def test_three_garbled_tries_per_hour_then_limit_then_the_hour_passes(h4):
    bad = CODE[:-1] + ("0" if CODE[-1] != "0" else "1")
    lefts = [h4.s.reserve_code_in(PEER_ADMIN, bad).get("left") for _ in range(3)]
    assert lefts == [2, 1, 0], f"счёт попыток: {lefts}"
    res = h4.s.reserve_code_in(PEER_ADMIN, CODE)
    assert not res["ok"] and res["why"] == "limit" and not h4.launched, "четвёртая попытка за час прошла"
    h4.advance(3601)
    assert h4.s.reserve_code_in(PEER_ADMIN, CODE)["ok"], "через час попытки не вернулись"


def test_a_wrong_code_reported_by_the_process_counts_and_reopens_the_window(h4, monkeypatch):
    assert h4.s.reserve_code_in(PEER_ADMIN, CODE)["ok"]
    monkeypatch.setattr(rsvzone, "open_status", lambda: {"step": "fail", "reason": "ключ пакета: SecretBox не снялся"})
    acts = h4.s.reserve_in_tick()
    assert h4.s.reserve_in() == "idle"
    assert h4.s.reserve_code_window()["open"], "после «не подошёл» приём кода не открыт снова"
    told = [n for n in notes(acts) if n.tg_id == PEER_ADMIN]
    assert len(told) == 1 and "Код не подошёл" in told[0].text and "Осталось попыток: 2" in told[0].text, told
    assert h4.s.reserve_code_in(PEER_ADMIN, CODE)["ok"], "после возврата окна верный по виду код не принят"


def test_a_process_failure_of_another_kind_reopens_the_window_without_a_try(h4, monkeypatch):
    assert h4.s.reserve_code_in(PEER_ADMIN, CODE)["ok"]
    monkeypatch.setattr(rsvzone, "open_status", lambda: {"step": "fail", "reason": "зона: veth не поднялся"})
    acts = h4.s.reserve_in_tick()
    told = [n for n in notes(acts) if n.tg_id == PEER_ADMIN]
    assert len(told) == 1 and "поднять не удалось" in told[0].text and "снова открыт" in told[0].text
    cw = h4.s.rsv_get("rsv_view")["code"]
    assert cw["open"] and not [t for t in cw.get("tries") or []], "сбой зоны засчитан как попытка кода"


def test_only_the_role_chat_handler_passes_a_code_to_the_reserve():
    """Код принимается только в чате роли: ни CLI, ни канал пары, ни бот
    основы на резерве не зовут reserve_code_in."""
    callers = []
    for base in ("awgbot", "tools"):
        for path in (ROOT / base).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "reserve_code_in":
                    callers.append(str(path.relative_to(ROOT)))
    assert sorted(set(callers)) == ["awgbot/bot/handlers/peercode.py"], f"код принимают ещё и здесь: {callers}"
