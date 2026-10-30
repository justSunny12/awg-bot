"""Аварийный код админа основы на резерве (codewin.reserve_code_in /
reserve_code_failed): только от аккаунта роли, только при открытом окне кода
(его открывает кандидат кворума: свой «не вижу», голосов не хватает), три
попытки в час, код транзитом в процесс открытия вместе с точкой и ящиком кода
линии — у резерва его нет ни в базе, ни в файлах; отказ процесса «ключ не
снялся» — попытка в счёт и окно снова открыто.

Мир — `Cand` из _reserve_vote_common: линия «Beta» с шлюзами b1, b2 (v = 3).

Цена ошибки: код, принятый не от админа основы (или при закрытом окне), — переезд по
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
from tests.integration._reserve_vote_common import (ADMIN, PEER_ADMIN, QBOX, RSV_Y, Cand, box, code_window_outside,
                                                    notes)

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["box"]

CODE = rsvkeys.code_encode(bytes(range(16)))          # верный по виду код (точка-заглушка)
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def h4(box, monkeypatch):
    """Окно кода вне решения: свой «не вижу», шлюзы «Beta» молчат — голосов нет."""
    monkeypatch.setattr(rsvzone, "teardown", lambda: "")
    monkeypatch.setattr(rsvzone, "wipe", lambda: "")
    box.peer_admin_opened()
    box.primary(False)
    box.cand = Cand(box)
    code_window_outside(box.cand)
    return box


def test_the_code_from_the_peer_admin_in_h4_goes_to_the_opening_process_in_transit(h4):
    res = h4.s.reserve_code_in(PEER_ADMIN, CODE.lower().replace("-", " "))
    assert res["ok"] and res["pending"], res
    assert h4.launched == [{"kind": "code", "code": CODE.replace("-", ""), "cpoint": [rsvkeys.RESERVE_X, RSV_Y],
                            "cbox": QBOX}], f"в процесс открытия не код с точкой и ящиком кода линии: {h4.launched}"
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


def test_a_code_without_the_window_is_refused(box):
    """Свой «не вижу», но шлюз видит основу — окна нет, код не принимается."""
    box.peer_admin_opened()
    box.primary(False)
    c = Cand(box)
    c.own("uns")
    c.view(1, "see")
    c.tick()
    assert not box.s.reserve_code_window()["open"], "окно кода открыто, хотя шлюз видит основу"
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


def test_a_gateway_seeing_the_primary_again_closes_the_window_with_the_majority_notice(h4):
    """Т-РН3: вернувшийся шлюз видит основу — окно закрыто, админу основы
    «Большинство снова видит «Beta» — переезд не нужен» один раз; имён шлюзов
    основы в тексте нет (второй стороне они не раскрываются)."""
    h4.hello(1)
    acts = h4.cand.view(1, "see") + h4.cand.tick()
    assert not h4.s.reserve_code_window()["open"], "шлюз видит основу — окно кода осталось открытым"
    told = [n for n in notes(acts) if n.tg_id == PEER_ADMIN]
    assert len(told) == 1, [n.text for n in notes(acts)]
    assert told[0].text == "ℹ️ Большинство снова видит «Beta» — переезд не нужен", told[0].text
    assert not notes(h4.cand.run(30)), "отбой окна повторился"


def test_own_see_back_closes_the_window(h4):
    h4.cand.own("see")
    told = [n for n in notes(h4.cand.tick()) if n.tg_id == PEER_ADMIN]
    assert not h4.s.reserve_code_window()["open"], "свой «вижу» — окно кода осталось открытым"
    assert len(told) == 1 and "переезд не нужен" in told[0].text, [n.text for n in told]


def test_gateways_back_with_no_close_the_window_as_votes_enough_not_as_seen_again(h4):
    """Окно вне решения открыто, шлюзы вернулись и тоже не видят основу —
    большинство «нет», переезд пойдёт сам: окно закрывается, и админу основы
    нужно сказать «Голосов хватило — переезд идёт сам, код не нужен», а не
    «Большинство снова видит» (сервер-то не видит никто — он поверит, что всё
    в порядке, и не будет готов к переезду)."""
    h4.hello(1)
    h4.hello(2)
    acts = h4.cand.view(1, "uns") + h4.cand.view(2, "uns") + h4.cand.tick()
    assert not h4.s.reserve_code_window()["open"], "большинство «нет» — окно кода осталось открытым"
    told = [n for n in notes(acts) if n.tg_id == PEER_ADMIN]
    assert len(told) == 1, [n.text for n in notes(acts)]
    assert "снова видит" not in told[0].text and "код не нужен" in told[0].text, \
        f"окно закрыто большинством «нет», а админу основы сказано другое: {told[0].text}"
