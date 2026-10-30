"""Гостевой канал шлюзов основы на стороне резерва (domain/services/reserve/
gwguest.py): сессия по `hello{slot, caps, nonce}` — первым словом `state` и
эпоха линии `qstate`; живость шлюза по счётчику стороны зоны и пробе
`ping`/`pong`; слово `state{serving|idle, move, e}` по смене; `qview`/`qshare`
уходят кворуму; слов матрицы (`seen`, `share`, `view`, `need`) резерв не
понимает и ничего по ним не делает; молчание в простое.

Сценарий — `_Box` и `Cand` из _reserve_vote_common: модельные часы, rx сторон
зоны, линия `b` второй стороны (кандидат — этот сервер).

Цена ошибки: шлюз, выпавший из живых по одной потерянной keepalive, — его «нет»
выпадает из счёта, и кворум не набирается; без `state` в начале сессии шлюз,
переподключившийся во время переезда, держит пересылку закрытой — его
пользователи без VPN; без `qstate` шлюз голосует под прежней эпохой; канал,
говорящий в простое, — маячок по ровному такту из сети шлюза; слово матрицы,
принятое резервом, — второй путь переезда мимо большинства.
"""
from __future__ import annotations

import pytest

from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import LINE, Cand, box, gw_sent

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["box"]


# ── начало сессии ───────────────────────────────────────────────────────────

def test_hello_gets_state_at_once_and_no_qstate_without_her_quorum(box):
    """Линии второй стороны нет — эпохи нет: шлюзу только `state{idle, move=0}`."""
    acts = box.hello(1)
    assert gw_sent(acts, "state") == [(1, {"state": "idle", "move": 0})], f"state в начале сессии не тот: {acts}"
    assert not gw_sent(acts, "qstate"), "qstate без включённого кворума второй стороны"
    assert box.alive(1), "шлюз после hello не на связи"


def test_hello_with_her_quorum_on_gets_the_line_epoch_first(box):
    """Кворум второй стороны включён, эпоха известна (7): `state` несёт её,
    следом `qstate{l, e, holder, r=cand}` — держатель линии она сама."""
    Cand(box, hello=False)
    acts = box.hello(1)
    assert [a[2] for a in acts] == ["state", "qstate"], f"первые слова сессии не state → qstate: {acts}"
    assert gw_sent(acts, "state")[0][1] == {"state": "idle", "move": 0, "e": 7}, gw_sent(acts, "state")
    assert gw_sent(acts, "qstate") == [(1, {"l": LINE, "e": 7, "holder": LINE, "r": "cand"})], gw_sent(acts, "qstate")


def test_hello_with_her_quorum_off_gets_no_qstate(box):
    Cand(box, hello=False)
    box.peer_line(active=False, why=["keys"])
    acts = box.hello(1)
    assert not gw_sent(acts, "qstate"), "qstate о линии, чей кворум выключен"


def test_hello_while_hosting_names_this_server_as_the_holder(box):
    Cand(box, hello=False)
    box.s.reserve_set_in("serving")
    acts = box.hello(2)
    assert gw_sent(acts, "state")[0][1]["state"] == "serving", "в начале сессии во время приёма — не serving"
    q = gw_sent(acts, "qstate")
    assert q and q[0][1]["holder"] == "self", f"держатель линии при её пользователях у меня — не я: {q}"


# ── живость: 39 / 40 / проба / 15 с ─────────────────────────────────────────

def test_a_gateway_stays_alive_through_39_seconds_of_silence_and_is_probed_at_40(box):
    box.hello(1)
    box.tick()                                          # первый замер счётчика — жизнь
    box.advance(39)
    acts = box.tick()
    assert box.alive(1) and not gw_sent(acts, "ping"), "проба или «нет связи» раньше 40 с тишины"
    box.advance(1)
    pings = gw_sent(box.tick(), "ping")
    assert len(pings) == 1 and pings[0][0] == 1 and len(pings[0][1]["n"]) == 16, f"на 40-й секунде нет пробы: {pings}"
    assert box.alive(1), "под подозрением шлюз уже «нет связи»"
    box.s.reserve_guest_in(1, "pong", {"n": pings[0][1]["n"]})
    box.advance(5)
    box.tick()
    assert box.alive(1) and box.s._gwv[1]["lv"].state == "ok", "ответ на пробу не снял подозрение"


def test_no_answer_for_15_seconds_marks_the_gateway_down_with_the_moment(box):
    box.hello(1)
    box.tick()
    box.advance(40)
    assert gw_sent(box.tick(), "ping")
    for _ in range(2):
        box.advance(5)
        box.tick()
        assert box.alive(1), "до конца пробы шлюз уже «нет связи»"
    box.advance(5)                                      # 55 с: 15 с пробы без ответа
    box.tick()
    assert not box.alive(1), "шлюз без ответа 15 с остался живым"
    assert box.s._gwv[1]["down_since"], "момент «нет связи» не записан"
    box.keepalive(1)
    box.advance(5)
    box.tick()
    assert box.alive(1) and box.s._gwv[1]["down_since"] == "", "keepalive после «нет связи» не вернул шлюз"


def test_one_lost_keepalive_does_not_take_the_gateway_down(box):
    """Пропущенный keepalive даёт пробу, следующий пакет снимает подозрение."""
    box.hello(1)
    box.tick()
    states = []
    for _ in range(8):                                  # 40 с тишины
        box.advance(5)
        box.tick()
        states.append(box.alive(1))
    box.keepalive(1)                                    # следующий keepalive через 45 с
    box.advance(5)
    box.tick()
    states.append(box.alive(1))
    assert all(states), f"одна потерянная keepalive — «нет связи»: {states}"


def test_a_closed_session_is_not_probed_and_comes_back_with_hello(box):
    box.hello(1)
    box.gone(1)
    assert not box.alive(1), "закрытая сессия считается живой"
    box.advance(60)
    assert not gw_sent(box.tick(), "ping"), "проба шлюзу без сессии"
    box.hello(1)
    assert box.alive(1)


def test_unpairing_forgets_the_gateways(box, monkeypatch):
    box.hello(1)
    monkeypatch.setattr(box.s, "reserve_paired", lambda: False)
    assert box.tick() == [] and box.s._gwv == {}, "без пары память шлюзов осталась"


# ── state по смене ──────────────────────────────────────────────────────────

def test_state_serving_goes_to_every_live_gateway_once_per_change(box):
    box.hello(1)
    box.hello(2)
    box.hello(3)
    box.gone(3)
    box.tick()
    box.s.reserve_set_in("taking", gen=3)
    states = gw_sent(box.tick(), "state")
    assert sorted(s for s, _ in states) == [1, 2], f"state{{serving}} не всем живым (или и закрытому): {states}"
    assert all(b == {"state": "serving", "move": 3} for _, b in states), states
    box.advance(5)
    assert not gw_sent(box.tick(), "state"), "state{serving} повторился без смены"
    box.s.reserve_set_in("idle", gen=3)
    back = gw_sent(box.tick(), "state")
    assert sorted(s for s, _ in back) == [1, 2] and all(b["state"] == "idle" for _, b in back), back


# ── слова шлюза ─────────────────────────────────────────────────────────────

def test_a_qview_goes_to_the_quorum_and_the_candidate_counts_it(box):
    c = Cand(box)
    c.own("uns")
    assert box.s.reserve_guest_in(1, "qview", {"l": LINE, "g": 1, "w": "uns", "e": 7, "since": "2026-10-30T10:00:00+03:00"}) == []
    assert box.s.reserve_quorum_gw_views(LINE).get("b1", {}).get("w") == "uns", "qview шлюза не дошёл до кворума"
    c.tick()
    assert box.s.reserve_quorum_candidate_view()["no"] == 2, "«нет» шлюза по гостевому каналу не в счёте кандидата"


@pytest.mark.parametrize("kind, body", [("seen", {"seen": ""}), ("share", {"share": "cd" * 16, "m_gen": 1}),
                                        ("view", {"seen": "", "voters": []}), ("need", {"case": "1", "since": ""})])
def test_matrix_words_change_nothing(box, kind, body):
    """Слова матрицы из прежнего агента (их и словарь не пустит) — резерв не
    отвечает и ничего не запускает."""
    Cand(box)
    box.primary(False)
    acts = box.s.reserve_guest_in(1, kind, body)
    assert acts == [] and not box.launched, f"слово {kind} дало действия: {acts}"
    assert not box.s.reserve_code_window()["open"]


# ── простой ─────────────────────────────────────────────────────────────────

def test_the_guest_channel_is_silent_for_an_hour_of_idle(box):
    """Без расписания: шлюзы на связи, keepalive раз в ~30 с, основа видна —
    за час ни одного прикладного сообщения ни шлюзам, ни основе."""
    box.hello(1)
    box.hello(2)
    box.tick()                                          # первый такт после сессий — исходная точка
    acts = []
    for i in range(720):
        box.advance(5)
        if i % 6 == 5:
            box.keepalive()
        acts += box.tick()
    assert not [a for a in acts if a and a[0] in ("gw", "send", "notify")], f"в простое канал говорил: {acts[:5]}"
