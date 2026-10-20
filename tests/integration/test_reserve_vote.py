"""Голосование шлюзов основы на резерве (domain/services/reserve/vote.py):
набор голосующих, живость шлюза по стороне зоны и пробе гостевого канала,
`need` → `share` → процесс открытия по двум долям, строка Н4 и окно
аварийного кода, `state` и `view` шлюзам, молчание в простое.

Сценарий — `_Box` из _reserve_vote_common: модельные часы, rx сторон зоны,
взгляд резерва на основу, журнал запусков процесса открытия (подмена).

Цена ошибки: шлюз, выпавший из набора по одной потерянной keepalive, —
случай 2 → Н4 и ложное Т-РУ7б со звуком админу основы; `need` шлюзу «вижу»
или в случае 2 — резерв собирает ключ при живой основе; доля без просьбы
или прежнего поколения — процесс открытия на чужом слове; Т-РУ7б дважды —
звонок ночью без причины; гостевой канал, говорящий в простое, — маячок по
ровному такту из сети шлюза.
"""
from __future__ import annotations

import itertools

import pytest

from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import (ADMIN, GW_SHARE, M_GEN, PEER_ADMIN, RSV_SHARE, box, gateway_point,
                                                    gw_sent, notes)

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["box"]

TABLE = {(False, "all_unseen"): "1", (False, "all_see"): "2", (False, "mixed"): "5", (False, "none"): "H4",
         (True, "all_unseen"): "3", (True, "mixed"): "4", (True, "all_see"): "6", (True, "none"): "H3"}


def _kind(views: list[str]) -> str:
    vals = set(views)
    if not vals:
        return "none"
    if vals == {"see"}:
        return "all_see"
    if vals == {"unseen"}:
        return "all_unseen"
    return "mixed"


def _reset(box) -> None:
    box.s.__dict__.pop("_rsv_gwviews", None)
    box.s.rsv_put("rsv_view", None)
    box.rx.clear()


# ── таблица истинности с недоступными и поколениями ─────────────────────────

def test_the_truth_table_with_unreachable_gateways_and_share_generations(box):
    """1–3 шлюза, у каждого: вижу / не вижу × доля текущего или прежнего
    поколения, или недоступен. Голосуют только с долей текущего поколения и
    на связи; итог — клетка матрицы по ним."""
    states = ("see", "unseen", "see_old", "unseen_old", "absent")
    checked = 0
    for sees in (False, True):
        for n in (1, 2, 3):
            for combo in itertools.product(states, repeat=n):
                _reset(box)
                box.primary(sees)
                for slot, st in zip((1, 2, 3), combo):
                    if st == "absent":
                        continue
                    box.hello(slot, sees=st.startswith("see"), m_gen=M_GEN if not st.endswith("_old") else M_GEN + 7)
                box.eval()
                voting = [st for st in combo if st in ("see", "unseen")]
                want = TABLE[(sees, _kind(voting))]
                assert box.case == want, (f"резерв {'видит' if sees else 'не видит'}, шлюзы {combo}: "
                                          f"случай {box.case}, а по матрице {want}")
                checked += 1
    assert checked == 2 * (5 + 25 + 125)


def test_a_gateway_with_a_share_of_the_previous_generation_does_not_vote(box):
    """Доля прежнего поколения процесс открытия не откроет: такой шлюз — не
    голосующий, и в случае 1 ему `need` не уходит."""
    box.primary(False)
    acts = box.hello(1, sees=False, m_gen=M_GEN + 1)
    assert box.voters == {} and box.case == "H4", "шлюз с долей прежнего поколения попал в набор"
    assert not gw_sent(acts, "need"), "need шлюзу с долей прежнего поколения"


# ── живость шлюза: 39 / 40 / проба / 15 с ───────────────────────────────────

def test_a_gateway_stays_in_the_set_through_39_seconds_of_silence_and_is_probed_at_40(box):
    box.primary(False)
    box.hello(1, sees=True)
    box.tick()                                          # первый замер счётчика — жизнь
    box.advance(39)
    acts = box.tick()
    assert box.voters == {1: "see"} and not gw_sent(acts, "ping"), "проба или выход из набора раньше 40 с тишины"
    box.advance(1)
    acts = box.tick()
    pings = gw_sent(acts, "ping")
    assert len(pings) == 1 and pings[0][0] == 1 and len(pings[0][1]["n"]) == 16, f"на 40-й секунде нет пробы: {acts}"
    assert box.voters == {1: "see"}, "под подозрением шлюз уже выпал из набора"
    box.s.reserve_guest_in(1, "pong", {"n": pings[0][1]["n"]})
    box.advance(5)
    box.tick()
    assert box.voters == {1: "see"} and box.case == "2", "ответ на пробу не вернул шлюз в набор"


def test_no_answer_for_15_seconds_takes_the_gateway_out_and_the_case_changes_in_the_same_tick(box):
    """Решение — синхронный пересчёт: шлюз недоступен — случай 2 → Н4 в том
    же такте, без ожидания сверх определения живости; Т-РУ7 — в нём же."""
    box.primary(False)
    box.hello(1, sees=True)
    box.tick()
    box.advance(40)
    assert gw_sent(box.tick(), "ping")
    for _ in range(2):
        box.advance(5)
        box.tick()
        assert box.case == "2", "до конца пробы случай сменился"
    box.advance(5)                                      # 55 с: 15 с пробы без ответа
    acts = box.tick()
    assert box.voters == {} and box.case == "H4", f"шлюз без ответа 15 с остался в наборе: {box.voters}, {box.case}"
    assert any(n.tg_id == ADMIN for n in notes(acts)), "Т-РУ7 не ушло в такт смены случая"


def test_one_lost_keepalive_does_not_change_the_case(box):
    """Пропущенный keepalive даёт пробу, следующий пакет снимает подозрение:
    случай 2 не становится Н4 ни на такт."""
    box.primary(False)
    box.hello(1, sees=True)
    box.tick()
    cases = []
    for _ in range(8):                                  # 40 с тишины
        box.advance(5)
        box.tick()
        cases.append(box.case)
    box.keepalive(1)                                    # следующий keepalive через 45 с
    box.advance(5)
    box.tick()
    cases.append(box.case)
    assert set(cases) == {"2"}, f"одна потерянная keepalive сменила случай: {cases}"
    assert box.voters == {1: "see"}


# ── need → share → процесс открытия ─────────────────────────────────────────

def test_need_goes_only_to_an_unseen_gateway_in_case_5(box):
    box.primary(False)
    box.hello(1, sees=True)
    acts = box.hello(2, sees=False)
    assert box.case == "5"
    assert [s for s, _ in gw_sent(acts, "need")] == [2], f"need не шлюзу «не вижу»: {gw_sent(acts, 'need')}"
    assert gw_sent(acts, "need")[0][1]["case"] == "5"


@pytest.mark.parametrize("sees,views,case", [
    (False, {1: True, 2: True}, "2"), (True, {1: False, 2: True}, "4"), (True, {1: True}, "6"),
    (True, {}, "H3"), (False, {}, "H4"), (True, {1: False, 2: False}, "3")])
def test_no_need_outside_cases_1_and_5(box, sees, views, case):
    """В 2, 4, 6, Н3, Н4 `need` не уходит; в 3 — только после молчания основы
    на просьбу (демпфер, test_reserve_rfblock)."""
    box.primary(sees)
    acts = []
    for slot, s in views.items():
        acts += box.hello(slot, sees=s)
    for _ in range(3):
        box.advance(5)
        box.keepalive()
        acts += box.tick()
    assert box.case == case
    assert not gw_sent(acts, "need"), f"need в случае {case}: {gw_sent(acts, 'need')}"


def test_need_is_repeated_to_the_next_unseen_gateway_after_30_seconds_without_a_share(box):
    box.primary(False)
    acts = box.hello(1, sees=False) + box.hello(2, sees=False)
    assert [s for s, _ in gw_sent(acts, "need")] == [1], "need ушёл не одному, первому «не вижу»"
    box.advance(29)
    box.keepalive()
    assert not gw_sent(box.tick(), "need"), "повтор need раньше 30 с"
    box.advance(1)
    box.keepalive()
    assert [s for s, _ in gw_sent(box.tick(), "need")] == [2], "без доли за 30 с — не спросил второго"


def test_a_share_on_need_opens_by_the_two_points_of_the_line(box):
    """Резерв + шлюз: точки x = 1 и x = слот + 1 той же прямой; вид — dead;
    шлюзам — state{serving} по смене."""
    box.primary(False)
    box.hello(1, sees=False)
    box.hello(2, sees=True)
    assert box.case == "5"
    acts = box.share(1)
    assert len(box.launched) == 1, f"процесс открытия не запущен по доле: {acts}"
    assert box.launched[0]["shares"] == [[1, RSV_SHARE], [gateway_point(1), GW_SHARE]]
    assert box.s.reserve_in() == "taking" and box.s.rsv_get("rsv_in")["kind"] == "dead"
    states = gw_sent(acts, "state")
    assert sorted(s for s, _ in states) == [1, 2] and all(b["state"] == "serving" for _, b in states), states
    assert not gw_sent(box.eval(), "state"), "state{serving} ушёл повторно без смены"


def test_a_share_without_a_request_is_not_taken(box):
    box.primary(True)
    box.hello(1, sees=False)
    box.share(1)
    assert not box.launched, "процесс открытия по доле без need"


def test_a_share_from_a_gateway_that_sees_the_primary_again_is_not_taken(box):
    box.primary(False)
    box.hello(1, sees=False)
    box.hello(2, sees=False)
    box.seen(1, True)                                   # после need шлюз снова видит основу
    box.share(1)
    assert not box.launched, "доля шлюза, который видит основу, открыла пакет"


def test_a_share_of_another_generation_is_not_taken(box):
    box.primary(False)
    box.hello(1, sees=False)
    box.share(1, m_gen=M_GEN + 1)
    assert not box.launched, "доля прежнего поколения пошла в процесс открытия"


def test_the_anti_bounce_interval_blocks_the_automatic_move(box):
    """Между автоматическими переездами ≥ min_interval_minutes: свежий конец
    автоматического — `need` не уходит, причина — строкой."""
    box.s.reserve_set_in("idle", ended_auto=True)
    box.primary(False)
    acts = box.hello(1, sees=False)
    assert box.case == "1" and not gw_sent(acts, "need")
    assert "не меньше 30 мин" in box.s.rsv_get("rsv_view")["auto_block"]


# ── Н4 и окно кода ──────────────────────────────────────────────────────────

def test_h4_tells_both_admins_in_the_same_tick_and_only_once(box):
    box.peer_admin_opened()
    box.primary(False)
    acts = box.tick()
    assert box.case == "H4"
    got = notes(acts)
    assert sorted(n.tg_id for n in got) == sorted([ADMIN, PEER_ADMIN]), f"Т-РУ7/Т-РУ7б не в такте t0: {got}"
    peer = next(n for n in got if n.tg_id == PEER_ADMIN)
    assert peer.keyboard == "peer_code" and peer.force_sound, "Т-РУ7б без кнопки кода или без звука"
    assert "аварийный код" in peer.text and not peer.text.endswith(".")
    assert box.s.reserve_code_window()["open"]
    again = []
    for _ in range(12):
        box.advance(5)
        again += box.tick()
    assert not notes(again), f"Т-РУ7/Т-РУ7б повторились в той же Н4: {notes(again)}"


def test_h4_without_the_peer_admin_ever_opening_the_bot_tells_only_the_reserve_admin(box):
    box.primary(False)
    got = notes(box.tick())
    assert [n.tg_id for n in got] == [ADMIN], "написали админу основы, который бот не открывал"
    assert "написать ему некуда" in got[0].text
    assert box.s.reserve_code_window()["open"], "окно кода не открыто без Т-РУ7б"


def test_a_gateway_back_in_h4_with_unseen_gets_need_and_closes_the_code_window(box):
    box.peer_admin_opened()
    box.primary(False)
    box.tick()
    acts = box.hello(1, sees=False)
    assert box.case == "1" and [s for s, _ in gw_sent(acts, "need")] == [1], "вернувшийся «не вижу» не получил need"
    assert not box.s.reserve_code_window()["open"], "приём кода открыт, хотя переезд идёт сам"
    told = [n for n in notes(acts) if n.tg_id == PEER_ADMIN]
    assert len(told) == 1 and "код не нужен" in told[0].text


def test_a_gateway_back_in_h4_with_see_closes_the_code_window_and_no_move(box):
    box.peer_admin_opened()
    box.primary(False)
    box.tick()
    acts = box.hello(1, sees=True)
    assert box.case == "2" and not gw_sent(acts, "need")
    assert not box.s.reserve_code_window()["open"], "приём кода остался открыт в случае 2"
    told = [n for n in notes(acts) if n.tg_id == PEER_ADMIN]
    assert len(told) == 1 and "переезд не нужен" in told[0].text


def test_the_code_window_is_never_open_outside_h4(box):
    for sees, views in ((False, {1: True}), (True, {}), (True, {1: False}), (False, {1: False})):
        _reset(box)
        box.primary(sees)
        for slot, s in views.items():
            box.hello(slot, sees=s)
        box.eval()
        assert not box.s.reserve_code_window()["open"], f"окно кода открыто в случае {box.case}"


# ── view и state шлюзам; молчание в простое ─────────────────────────────────

def test_hello_gets_view_and_state_at_once(box):
    acts = box.hello(1, sees=True)
    assert gw_sent(acts, "view") and gw_sent(acts, "state")
    assert gw_sent(acts, "state")[0][1] == {"state": "idle", "move": 0}


def test_case_4_view_goes_to_the_primary_once(box):
    box.primary(True)
    acts = box.hello(2, sees=True) + box.hello(1, sees=False)       # 6 → 4: основе — только 4
    views = [a[2] for a in acts if a[0] == "send" and a[1] == "view"]
    assert box.case == "4" and len(views) == 1 and views[0]["case"] == "4", views
    names = {v["name"]: v["see"] for v in views[0]["voters"]}
    assert names == {"NASPi": False, "Pi5": True}
    more = []
    for _ in range(6):
        box.advance(5)
        box.keepalive()
        more += box.tick()
    assert not [a for a in more if a[0] == "send" and a[1] == "view"], "view основе повторился без смены"


def test_the_guest_channel_is_silent_for_an_hour_of_idle(box):
    """Без расписания: шлюзы на связи, keepalive раз в ~30 с, основа видна —
    за час ни одного прикладного сообщения ни шлюзам, ни основе."""
    box.primary(True)
    box.hello(1, sees=True)
    box.hello(2, sees=True)
    acts = []
    for i in range(720):
        box.advance(5)
        if i % 6 == 5:
            box.keepalive()
        acts += box.tick()
    assert not [a for a in acts if a and a[0] in ("gw", "send", "notify")], f"в простое канал говорил: {acts[:5]}"


def test_the_opening_by_shares_ensures_the_zone_shell_first_and_says_share_in_st(box, monkeypatch):
    """Переезд по долям: оболочка зоны поднимается до процесса открытия, а
    `in_by = share` уходит основе в st — по нему основа после возврата
    меняет поколение ключа."""
    order = []
    monkeypatch.setattr(box.s, "reserve_zone_shell_ensure", lambda: order.append("shell") or True)
    real = box._launch
    monkeypatch.setattr(box.s, "_rsvopen_launch", lambda payload: order.append("open") or real(payload))
    box.primary(False)
    box.hello(1, sees=False)
    box.hello(2, sees=True)                             # случай 5: need — сразу
    box.share(1)
    assert box.launched, "переезд по долям не начался"
    assert order[:2] == ["shell", "open"], order
    assert box.s.rsv_get("rsv_in").get("by") == "share" and box.s.reserve_st_body()["in_by"] == "share"


def test_need_carries_the_generation_of_the_share_of_the_package(box):
    """Шлюз сверяет поколение своей доли с m_gen в need — без поля сверять нечего."""
    box.primary(False)
    acts = box.hello(1, sees=False) + box.hello(2, sees=True)
    need = gw_sent(acts, "need")
    assert need and all(b.get("m_gen") == M_GEN for _s, b in need), need
