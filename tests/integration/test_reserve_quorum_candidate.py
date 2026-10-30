"""Правило кандидата кворума и исполнение решения на стороне резерва
(domain/services/reserve/quorum/candidate.py; концепт «кворум» §3.5, §3.6,
§3.7.2, §3.7.4): кандидат о линии второй стороны — свой взгляд из зонда к её
AWG, взгляды её шлюзов — `qview` по живому гостевому каналу; опрос `qask`,
снятие `qrel`, решение — `qtake` всем «не видящим», затем просьба `ask{q}`
лидеру, без ответа — точки `qneed` → `qshare` и процесс открытия по ящику
кворума, точек нет — окно аварийного кода; на просьбу кворума лидер отдаёт
эпоху путём кнопки (quorum/inbound.reserve_ask_in, case "q").

Мир — резерв «Alpha» (сторона `a`) в паре с «Beta» (`Cand` из tests/integration/
_reserve_vote_common.py): линия `b` с составом b, a, b1, b2 (голосующие у
лидера `b` — a, b1, b2; v = 3, t = 2), шлюзы «Beta» — слоты 1 и 2 на гостевом
канале. Свой взгляд кандидата — машина голосующего зонда `awgqbh` с
выставленным взглядом (пробы и ответы — тесты этапа зондов); точки и ящики
линии `b` — словом `qkeys`, как их шлёт основа. Процесс открытия — подмена
(журнал запусков box.launched); часы — модельные.

Цена ошибки: кандидат забирает пользователей без большинства — два живых
бота у одних пользователей; не забирает при большинстве — пользователи без
VPN-бота до ручного кода; «нет» мёртвого канала в счёте — переезд по
устаревшему слову; `qrel` не дошёл до сказавшего «нет» — его пробы лидеру
идут `held` до 10 минут, и живой лидер лишается кворума зря; точка чужого
поколения в сборе — процесс открытия падает уже после `qtake`, а голосующие
приняли эпоху, которой нет.
"""
from __future__ import annotations

import pytest

from awgbot.core import settings
from awgbot.util import rsvkeys

from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import (ASK, GW1_Y, GW2_Y, LINE, MEMBERS, NEED2, Q_GEN, QBOX, RF, RSV_Y, T_UP,
                                                    Cand as _Cand, box, gw_sent, sent)

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["box", "_Cand", "ASK", "LINE", "MEMBERS", "NEED2", "RF", "T_UP"]


@pytest.fixture()
def c(box, monkeypatch):
    return _Cand(box, monkeypatch)


def _majority(c, *, both: bool = True) -> list[tuple]:
    """Свой «не вижу» и «нет» шлюзов: первый такт после этого."""
    c.own("uns")
    c.view(1, "uns")
    if both:
        c.view(2, "uns")
    return c.tick()


def _taken(c) -> list[tuple]:
    """Большинство «нет» держится T_up — действия такта решения."""
    acts = _majority(c)
    acts += c.run(T_UP)
    return acts


# ── опрос ───────────────────────────────────────────────────────────────────

def test_own_uns_and_one_gateway_uns_asks_the_other_gateway(c):
    acts = _majority(c, both=False)
    assert gw_sent(acts, "qask") == [(2, {"l": LINE, "g": 1})], "второго шлюза не опросили"
    assert not gw_sent(acts, "qtake")


def test_own_uns_without_any_gateway_no_does_not_ask(c):
    """Концепт «кворум» §3.5.1: свой «не вижу» без единого «нет» шлюзов — чаще
    всего сеть самого кандидата; опрос лишний."""
    c.own("uns")
    acts = c.tick() + c.run(T_UP + 10)
    assert not gw_sent(acts, "qask") and not gw_sent(acts, "qtake"), acts


def test_own_see_does_not_ask(c):
    c.view(1, "uns")
    acts = c.tick() + c.run(30)
    assert not gw_sent(acts, "qask"), "кандидат, сам видящий лидера, опрашивает шлюзы"


# ── решение ─────────────────────────────────────────────────────────────────

def test_majority_of_no_for_t_up_takes_and_asks_the_leader(c):
    acts = _majority(c)
    acts += c.run(T_UP - 5)
    assert not gw_sent(acts, "qtake"), "«забираю» раньше T_up"
    acts = c.run(5)
    assert sorted(s for s, _ in gw_sent(acts, "qtake")) == [1, 2], "qtake ушёл не всем «не видящим»"
    assert all(b == {"l": LINE, "e": 7} for _, b in gw_sent(acts, "qtake")), gw_sent(acts, "qtake")
    asks = sent(acts, "ask")
    assert len(asks) == 1 and asks[0]["case"] == "q", "просьба лидеру по кворуму не ушла"
    assert set(asks[0]) == {"case", "since", "kind"}, f"в ask{{q}} поля сверх словаря пары: {asks[0]}"
    assert asks[0]["kind"] == "dead", "свой «не вижу» — вид переезда dead (Р6)"
    assert c.stage() == "ask"
    assert not c.b.launched, "процесс открытия до ответа лидера"


def test_take_is_once_per_decision(c):
    acts = _taken(c)
    assert len(sent(acts, "ask")) == 1
    more = c.run(30)
    assert not gw_sent(more, "qtake") and not sent(more, "ask"), "«забираю» повторяется каждый такт"


def test_an_open_block_refuses_with_qrel_and_the_card_line(c):
    settings.set_value("app.reserve.ready", False)
    acts = _taken(c)
    assert not gw_sent(acts, "qtake") and not sent(acts, "ask"), "открытие не пускают, а «забираю» ушло"
    assert sorted(s for s, _ in gw_sent(acts, "qrel")) == [1, 2], "отказ предусловий — без qrel сказавшим «нет»"
    assert c.s.rsv_get("rsv_view").get("auto_block") == "готовность выключена"


def test_the_zone_taken_by_the_go_resets_the_candidate(c):
    """Лидер ответил согласием — пошло открытие по `go`: исполнение кворума
    больше не нужно, и точки по нему просить нельзя."""
    _taken(c)
    c.s.reserve_set_in("taking")
    acts = c.run(ASK + NEED2 + 10)
    assert not gw_sent(acts, "qneed"), "открытие уже идёт, а кандидат просит точки"
    assert c.stage() == "" and not c.s.reserve_code_window()["open"]


def test_no_answer_for_ask_seconds_asks_the_points(c):
    _taken(c)
    acts = c.run(ASK - 5)
    assert not gw_sent(acts, "qneed"), "точки раньше ask_seconds"
    acts = c.run(5)
    needs = gw_sent(acts, "qneed")
    assert sorted(s for s, _ in needs) == [1, 2], "qneed ушёл не всем «не видящим»"
    assert all(b["q_gen"] == Q_GEN and b["l"] == LINE for _, b in needs), needs
    assert c.stage() == "points"


def _points_stage(c) -> None:
    _taken(c)
    c.run(ASK)
    assert c.stage() == "points"


def test_a_gateway_point_and_my_own_open_by_the_quorum_box(c):
    _points_stage(c)
    c.share(1)
    assert len(c.b.launched) == 1, "t точек (шлюз + своя при своём «не вижу») — процесс открытия не запущен"
    p = c.b.launched[0]
    assert p["kind"] == "dead", p
    assert p["qpoints"] == [[rsvkeys.RESERVE_X, RSV_Y], [rsvkeys.gateway_x(1), GW1_Y]], p
    assert p["qbox"] == QBOX
    assert c.s.reserve_in() == "taking"
    assert c.s.rsv_get("rsv_view").get("moved_by", {}).get("quorum") is True


def test_a_point_of_another_generation_is_refused(c):
    _points_stage(c)
    c.share(1, q_gen=Q_GEN - 1)
    assert not c.b.launched, "точка прежнего многочлена пошла в сбор"


def test_a_point_with_a_wrong_x_or_length_is_refused(c):
    _points_stage(c)
    c.s.reserve_guest_in(1, "qshare", {"l": LINE, "q_gen": Q_GEN, "x": rsvkeys.gateway_x(2), "y": GW1_Y})
    c.s.reserve_guest_in(1, "qshare", {"l": LINE, "q_gen": Q_GEN, "x": rsvkeys.gateway_x(1), "y": "cd" * 8})
    assert not c.b.launched, "точка не своего x или не той длины пошла в сбор"


def test_a_point_without_a_request_is_refused(c):
    _majority(c)
    c.share(1)
    assert not c.b.launched, "точка без qneed открыла зону"


def test_no_points_for_twice_need_retry_opens_the_code_window(c):
    _points_stage(c)
    c.run(NEED2 - 5)
    assert not c.s.reserve_code_window()["open"], "окно кода раньше 2 × NEED_RETRY_S"
    c.run(5)
    assert c.s.reserve_code_window()["open"], "точек нет, свой «не вижу» — окно аварийного кода не открылось"
    assert c.stage() == "code"


# ── снятие ──────────────────────────────────────────────────────────────────

def test_own_see_back_drops_the_majority_and_releases_who_said_no(c):
    """Свой взгляд снова «вижу» — большинство «нет» распалось, qrel уходит всем
    голосующим с живым каналом, а не только тем, кто сейчас «не вижу»:
    обязательство «нет» держит и тот, кто уже снова видит лидера (шлюз 2 —
    после qask), и без qrel его пробы лидеру идут `held` до срока."""
    _majority(c, both=False)                            # ушёл qask шлюзу 2
    c.own("see")
    acts = c.tick()
    assert sorted(gw_sent(acts, "qrel")) == [(1, {"l": LINE, "e": 7})], \
        f"«нет» ниже половины — qrel не всем голосующим с живым каналом: {gw_sent(acts, 'qrel')}"


def test_a_gateway_that_said_no_and_now_sees_still_gets_qrel(c):
    """Концепт «кворум» §3.5.1: «если до этого ушёл qask, кандидат шлёт qrel
    всем, кто сказал «нет»». Шлюз, сказавший «нет» и снова видящий лидера,
    держит обязательство: без qrel его пробы лидеру идут `held` до 10 минут."""
    _majority(c, both=False)
    c.view(1, "see")
    acts = c.tick()
    assert (1, {"l": LINE, "e": 7}) in gw_sent(acts, "qrel"), \
        f"шлюз 1 сказал «нет», вернулся к «вижу» — qrel ему не ушёл: {gw_sent(acts, 'qrel')}"


def test_gateways_seeing_again_close_the_code_window(c):
    _points_stage(c)
    c.run(NEED2)
    assert c.s.reserve_code_window()["open"]
    c.view(1, "see")
    c.view(2, "see")
    c.tick()
    assert not c.s.reserve_code_window()["open"], "шлюзы снова видят лидера — окно кода осталось открытым"
    assert c.stage() == ""


# ── кто в счёте ─────────────────────────────────────────────────────────────

def test_the_no_of_a_dead_gateway_channel_does_not_count(c):
    c.b.gone(2)
    c.own("uns")
    c.view(2, "uns")
    acts = c.tick() + c.run(T_UP + 10)
    assert not gw_sent(acts, "qtake") and not gw_sent(acts, "qask"), "«нет» шлюза без живого канала в счёте"
    assert c.s.reserve_quorum_candidate_view()["no"] == 1


def test_hosting_her_users_there_is_no_candidate(c):
    c.s.reserve_set_in("serving")
    acts = _majority(c) + c.run(T_UP + 10)
    assert acts == [] and c.s.reserve_quorum_candidate_view() == {}, "кандидат о линии, чьих пользователей я несу"


def test_without_her_quorum_there_is_no_candidate(c):
    c.s.rsv_update("rsv_peer_state", quo={"l": LINE, "g": 1, "members": list(MEMBERS), "active": False, "why": ["keys"]})
    acts = _majority(c) + c.run(T_UP + 10)
    assert acts == [], "кандидат при выключенном кворуме второй линии"


# ── случай 3 ────────────────────────────────────────────────────────────────

def test_case_3_takes_only_after_rf_block_and_opens_by_gateway_points_only(c):
    """Концепт «кворум» §3.5.2: свой «вижу», шлюзы не видят — ждём T_up_rf;
    своя точка в сбор не идёт (§3.6)."""
    c.view(1, "uns")
    c.view(2, "uns")
    acts = c.tick() + c.run(RF - 10)
    assert not gw_sent(acts, "qtake"), "случай 3 забран раньше rf_block_minutes"
    assert not gw_sent(acts, "qask"), "кандидат, сам видящий лидера, опрашивает"
    acts = c.run(10)
    assert sorted(s for s, _ in gw_sent(acts, "qtake")) == [1, 2], "случай 3 не забран через rf_block_minutes"
    c.run(ASK)
    assert c.stage() == "points"
    c.share(1)
    assert not c.b.launched, "своя точка пошла в сбор при своём «вижу»"
    c.share(2)
    assert len(c.b.launched) == 1
    assert c.b.launched[0]["qpoints"] == [[rsvkeys.gateway_x(1), GW1_Y], [rsvkeys.gateway_x(2), GW2_Y]]


def test_case_3_turning_own_uns_takes_by_the_short_timer(c):
    """Свой взгляд стал «не вижу» в tb — забираем не позже tb + T_up."""
    c.view(1, "uns")
    c.view(2, "uns")
    c.tick()
    c.run(60)
    c.own("uns")
    acts = c.run(T_UP + 5)
    assert sorted(s for s, _ in gw_sent(acts, "qtake")) == [1, 2], "после своего «не вижу» ждём полный rf_block"


# ── лидер на просьбу кворума ────────────────────────────────────────────────

@pytest.fixture()
def leader(services, rsv, monkeypatch):
    rc.pair_row(services)
    moves: list[dict] = []
    state = {"why": ""}
    monkeypatch.setattr(services, "reserve_move_check", lambda: state["why"])
    monkeypatch.setattr(services, "reserve_move_start", lambda **kw: moves.append(kw) or {})
    return services, moves, state


SINCE = "2026-10-30T10:05:00+03:00"


def test_the_leader_asked_by_the_quorum_hands_over_by_the_button_path(leader):
    svc, moves, _ = leader
    acts = svc.reserve_ask_in("q", SINCE)
    assert acts == [("live",)], acts
    assert moves == [{"kind": "dead", "dead_since": SINCE}], "по просьбе кворума переезд не начат путём кнопки"


def test_the_busy_leader_answers_busy_to_the_quorum(leader):
    svc, moves, state = leader
    state["why"] = "идёт переезд пользователей этого сервера"
    acts = svc.reserve_ask_in("q", SINCE)
    assert acts == [("send", "ask_no", {"reason": "busy"})], acts
    assert moves == [], "занятый лидер всё равно начал переезд"
    assert not [a for a in acts if a[0] == "notify"], "busy по кворуму — без уведомления"




def test_an_ask_of_another_case_is_neither_answered_nor_acted_on(leader):
    """`ask` знает только случай кворума `q`: прежние `3`/`nogw` (просьбы матрицы)
    — ни переезда, ни ответа."""
    svc, moves, _ = leader
    for case in ("3", "nogw", "H4", ""):
        assert svc.reserve_ask_in(case, SINCE) == [], f"ask{{{case}}} дал действия"
    assert moves == [], "по просьбе матрицы начат переезд"


# ── busy лидера: повтор просьбы ─────────────────────────────────────────────

def test_a_busy_leader_is_asked_again_after_ask_seconds_up_to_three_times_then_points(c):
    """`ask_no{busy}` — у лидера плановая пауза: кандидат ждёт `ask_seconds` и
    просит снова, до трёх раз; третий «занят» — по точкам. Сразу по точкам —
    переезд мимо живого лидера, который через минуту отдал бы эпоху сам."""
    _taken(c)
    asks = 1
    for n in range(1, 4):
        assert c.s.reserve_ask_no_in("busy") == []
        early = c.run(ASK - 5)
        assert not sent(early, "ask") and not gw_sent(early, "qneed"), f"повтор {n} раньше ask_seconds"
        more = c.run(5)
        if n < 3:
            assert len(sent(more, "ask")) == 1 and sent(more, "ask")[0] == {"case": "q", "since": sent(more, "ask")[0]["since"],
                                                                            "kind": "dead"}, \
                f"после «занят» {n} просьба не повторена: {more}"
            assert c.stage() == "ask"
            asks += 1
        else:
            assert not sent(more, "ask"), "после третьего «занят» просьба повторена ещё раз"
            assert sorted(s for s, _ in gw_sent(more, "qneed")) == [1, 2], "после третьего «занят» — не по точкам"
            assert c.stage() == "points"
    assert asks == 3, "просьб лидеру не три"


def test_busy_outside_the_ask_stage_changes_nothing(c):
    _majority(c)
    assert c.s.reserve_ask_no_in("busy") == []
    assert "busy" not in (c.s.__dict__.get("_quo_exec") or {}), "«занят» без просьбы засчитан"
    _taken(c)
    c.run(ASK)
    assert c.stage() == "points"
    c.s.reserve_ask_no_in("busy")
    assert c.stage() == "points" and not sent(c.run(ASK + 5), "ask"), "«занят» на стадии точек вернул просьбу"


def test_without_the_pair_link_a_busy_leader_is_not_asked_again(c):
    _taken(c)
    c.s.reserve_ask_no_in("busy")
    c.b.online = False
    acts = c.run(ASK + 5)
    assert not sent(acts, "ask") and gw_sent(acts, "qneed"), "без линка пары повтор просьбы вместо точек"
