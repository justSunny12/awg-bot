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


def test_a_garbled_code_is_not_a_try(h4):
    """Не код (контрольная сумма не сошлась) — не попытка подбора: ответ
    «не похоже на код», счёт не тронут, ничего не запущено."""
    bad = CODE[:-1] + ("0" if CODE[-1] != "0" else "1")
    for _ in range(5):
        res = h4.s.reserve_code_in(PEER_ADMIN, bad)
        assert (res["ok"], res["why"], res["left"]) == (False, "garbled", 3), res
    assert not h4.launched and h4.s.reserve_code_window()["open"]
    assert h4.s.reserve_code_in(PEER_ADMIN, CODE)["ok"], "опечатки съели попытки верного кода"


def test_three_wrong_codes_per_hour_then_limit_then_the_hour_passes(h4, monkeypatch):
    """Попытки считают только отказы процесса «ключ не снялся»: три за час —
    приём закрыт до конца часа."""
    real_status = rsvzone.open_status
    lefts = []
    for _ in range(3):
        monkeypatch.setattr(rsvzone, "open_status", real_status)
        assert h4.s.reserve_code_in(PEER_ADMIN, CODE)["ok"]
        monkeypatch.setattr(rsvzone, "open_status", lambda: {"step": "fail", "reason": "ключ пакета: SecretBox не снялся"})
        h4.s.reserve_in_tick()
        lefts.append(len(h4.s._code_tries(dict(h4.s.rsv_get("rsv_view").get("code") or {}))))
    assert lefts == [1, 2, 3], f"счёт попыток: {lefts}"
    monkeypatch.setattr(rsvzone, "open_status", real_status)
    launched = len(h4.launched)
    res = h4.s.reserve_code_in(PEER_ADMIN, CODE)
    assert not res["ok"] and res["why"] == "limit" and len(h4.launched) == launched, "четвёртая попытка за час прошла"
    h4.advance(3601)
    assert h4.s.reserve_code_in(PEER_ADMIN, CODE)["ok"], "через час попытки не вернулись"


def test_a_wrong_code_reported_by_the_process_counts_and_reopens_the_window(h4, monkeypatch):
    """Итог не успел в ответ на сообщение (pending) — отказ процесса приходит
    тактом и уведомлением; следующий ввод — уже новое открытие со своим ходом."""
    real_status = rsvzone.open_status
    assert h4.s.reserve_code_in(PEER_ADMIN, CODE)["ok"]
    monkeypatch.setattr(rsvzone, "open_status", lambda: {"step": "fail", "reason": "ключ пакета: SecretBox не снялся"})
    acts = h4.s.reserve_in_tick()
    assert h4.s.reserve_in() == "idle"
    assert h4.s.reserve_code_window()["open"], "после «не подошёл» приём кода не открыт снова"
    told = [n for n in notes(acts) if n.tg_id == PEER_ADMIN]
    assert len(told) == 1 and "Код не подошёл" in told[0].text and "Осталось попыток: 2" in told[0].text, told
    monkeypatch.setattr(rsvzone, "open_status", real_status)   # новое открытие пишет свой ход (launching)
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


def test_the_package_opened_by_the_code_is_said_as_code_in_st(h4):
    """Открыт кодом — `in_by = code`: после возврата основа меняет поколение
    ключа и просит сменить токен DNS (код видели, конверт DNS раскрыт)."""
    assert h4.s.reserve_code_in(PEER_ADMIN, CODE)["ok"]
    assert h4.s.rsv_get("rsv_in").get("by") == "code" and h4.s.reserve_st_body()["in_by"] == "code"


def test_the_admin_of_both_servers_enters_the_code_as_himself(h4):
    """Один человек — админ обоих: роль не наступает, код он вводит в своём
    разделе — от ADMIN_ID код принимается только тогда."""
    rc.pair_row(h4.s, admin=ADMIN)
    assert h4.s.reserve_code_window()["open"]
    res = h4.s.reserve_code_in(ADMIN, CODE)
    assert res["ok"] and len(h4.launched) == 1, res


@pytest.mark.parametrize("sees, why_text", [(True, "видит сервер — переезд не нужен"),
                                           (False, "не видит сервер — переезд идёт сам")])
def test_the_closing_notice_names_the_gateway_that_came_back(h4, sees, why_text):
    """Т-РН3: окно закрыл вернувшийся шлюз — в тексте он по номеру слота
    («Шлюз 1 снова на связи»), а не имя сервера основы. Имён шлюзов основы у
    резерва нет: они второй стороне не раскрываются."""
    acts = h4.hello(1, sees=sees)
    acts += h4.tick()
    told = [n for n in notes(acts) if n.tg_id == PEER_ADMIN]
    assert len(told) == 1, [n.text for n in notes(acts)]
    assert told[0].text.startswith("ℹ️ Шлюз 1 снова на связи") and why_text in told[0].text, told[0].text
    assert "«Beta»" not in told[0].text and "NASPi" not in told[0].text, told[0].text


def test_the_closing_notice_names_the_primary_when_it_came_back_itself(h4):
    h4.primary(True)
    told = [n for n in notes(h4.tick()) if n.tg_id == PEER_ADMIN]
    assert len(told) == 1 and "«Beta» снова на связи" in told[0].text, [n.text for n in told]
