"""Правило кандидата на резерве — ветви, которых нет в test_reserve_quorum_candidate
(domain/services/reserve/quorum/candidate.py; концепт «кворум» §3.5, §3.6, Т-К21):
окно аварийного кода вне решения (свой «не вижу», никто не видит, голосов
мало), снятие исполнения своим «вижу» (случай 3 — своим таймером), «забираю»
без линка пары — сразу по точкам, повтор `qneed` при своём «вижу», снятая
строка отказа предусловий, эпоха своего зонда e + 1, смена состава второй
линии на лету, кривой состав.

Мир — резерв «Alpha» (сторона `a`) в паре с «Beta»: линия `b`, состав b, a,
b1, b2 (у лидера `b` голосующие a, b1, b2; v = 3, t = 2), шлюзы «Beta» — слоты
1 и 2 на гостевом канале (`_Cand` из test_reserve_quorum_candidate.py: свой
взгляд — машина зонда `awgqbh` с выставленным взглядом, точки линии — словом
`qkeys`, процесс открытия — подмена, часы — модельные).

Цена ошибки: окно кода не открывается при своём «не вижу» и молчащих шлюзах —
админ основы не может ввести код, хотя переехать иначе нельзя; открывается при
чьём-то «вижу» — код принимается при живой основе; исполнение, не снятое
своим «вижу», — переезд с живой основы по короткому таймеру вместо 15 минут
случая 3; без линка пары кандидат ждёт ответа на просьбу, которую некому
передать, — лишняя минута без VPN-бота у пользователей.
"""
from __future__ import annotations

import pytest

from awgbot.core import config

from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import box, gw_sent, notes, sent
from tests.integration.test_reserve_quorum_candidate import ASK, LINE, MEMBERS, NEED2, RF, T_UP, _Cand

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["box"]


@pytest.fixture()
def c(box, monkeypatch):
    return _Cand(box, monkeypatch)


def _code(c) -> dict:
    return dict(c.s.rsv_get("rsv_view").get("code") or {})


def _taken(c) -> list[tuple]:
    c.own("uns")
    c.view(1, "uns")
    c.view(2, "uns")
    acts = c.tick() + c.run(T_UP)
    assert gw_sent(acts, "qtake"), "подготовка: «забираю» не отдано"
    return acts


# ── окно кода вне решения (Т-К21) ───────────────────────────────────────────

def test_own_uns_with_silent_gateways_opens_the_code_window_and_tells_both_admins(c):
    """Свой «не вижу», шлюзы основы молчат (их локальная сеть обесточена): голосов
    для переезда нет и не будет — приём аварийного кода открыт, Т-К21 админу резерва
    и (он открывал бота) админу основы с кнопкой кода, один раз."""
    c.b.peer_admin_opened(True)
    c.own("uns")
    acts = c.tick()
    cw = _code(c)
    assert cw.get("open") and cw.get("by") == "quorum" and cw.get("told"), cw
    ns = notes(acts)
    by = {n.tg_id: n for n in ns}
    assert set(by) == {int(config.ADMIN_ID), rc.PEER_ADMIN}, ns
    mine = by[int(config.ADMIN_ID)]
    assert mine.text.startswith("⚠️ «Beta» не отвечает с ") and "не видят 1 из 3, нужно 2" in mine.text, mine.text
    assert "бот написал ему" in mine.text
    peer = by[rc.PEER_ADMIN]
    assert peer.keyboard == "peer_code" and peer.force_sound and peer.text.startswith("🆘 «Alpha» не видит «Beta»"), peer
    assert notes(c.run(60)) == [], "Т-К21 повторяется на каждом такте"


def test_without_the_peer_admin_only_my_admin_is_told_that_nobody_can_be_written_to(c):
    c.b.peer_admin_opened(False)
    c.own("uns")
    ns = notes(c.tick())
    assert [n.tg_id for n in ns] == [int(config.ADMIN_ID)], ns
    assert "написать ему некуда" in ns[0].text
    assert _code(c).get("open") and not _code(c).get("told")


def test_a_single_seeing_gateway_keeps_the_code_window_shut(c):
    c.own("uns")
    c.view(1, "see")
    acts = c.tick() + c.run(30)
    assert not _code(c).get("open"), "окно кода открыто, хотя шлюз основы её видит"
    assert notes(acts) == []


def test_own_see_keeps_the_code_window_shut(c):
    c.view(1, "uns")
    c.view(2, "uns")
    c.tick()
    assert not c.s.reserve_code_window()["open"], "кандидат, сам видящий основу, открыл приём кода"


def test_a_gateway_seeing_again_closes_the_quorum_code_window_and_tells_the_peer_admin(c):
    c.b.peer_admin_opened(True)
    c.own("uns")
    c.tick()
    c.view(2, "see")
    acts = c.tick()
    cw = _code(c)
    assert not cw.get("open") and cw.get("closed_why") == "quorum", cw
    ns = notes(acts)
    assert [n.tg_id for n in ns] == [rc.PEER_ADMIN], ns
    assert ns[0].text == "ℹ️ Большинство снова видит «Beta» — переезд не нужен", ns[0].text


def test_the_closed_window_without_a_told_admin_says_nothing(c):
    c.b.peer_admin_opened(False)
    c.own("uns")
    c.tick()
    c.own("see")
    assert notes(c.tick()) == []
    assert not _code(c).get("open")


def test_no_code_window_while_hosting_or_taking(c):
    c.s.reserve_set_in("taking")
    c.own("uns")
    c.tick()
    assert not _code(c).get("open"), "окно кода, когда открытие уже идёт"


def test_the_window_reopened_in_the_same_episode_tells_only_the_peer_admin_silently(c):
    """Эпизод — момент своего «не вижу»: окно закрыл шлюз «вижу», его сессия
    оборвалась — голосов снова нет, окно открыто снова, админу резерва повтора нет, админу основы — без
    звука (код ему снова нужен)."""
    c.b.peer_admin_opened(True)
    c.own("uns")
    c.tick()
    c.view(1, "see")
    c.tick()
    assert not _code(c).get("open")
    c.b.advance(5)
    c.b.gone(1)
    acts = c.tick()
    assert _code(c).get("open"), "окно не открылось снова"
    ns = notes(acts)
    assert [n.tg_id for n in ns] == [rc.PEER_ADMIN], f"повтор того же эпизода админу резерва: {ns}"
    assert ns[0].silent and not ns[0].force_sound and ns[0].keyboard == "peer_code", ns[0]


def test_a_new_own_unseen_is_a_new_episode_told_again(c):
    c.b.peer_admin_opened(True)
    c.own("uns")
    c.tick()
    c.own("see")
    c.tick()
    c.b.advance(30)
    c.own("uns")
    acts = c.tick()
    by = {n.tg_id: n for n in notes(acts)}
    assert set(by) == {int(config.ADMIN_ID), rc.PEER_ADMIN}, f"новый эпизод не сказан обоим админам: {by}"
    assert by[rc.PEER_ADMIN].force_sound, "новый эпизод админу основы — без звука"


# ── исполнение: снятие своим «вижу» ─────────────────────────────────────────

def test_own_see_after_the_decision_drops_the_execution_and_waits_the_long_timer(c):
    """Решение было при своём «не вижу»; свой взгляд вернулся, а шлюзы основы по-прежнему
    не видят — это случай 3: исполнение снято, окно кода закрыто, следующее «забираю»
    не раньше T_up_rf."""
    _taken(c)
    assert c.stage() == "ask"
    c.own("see")
    acts = c.tick()
    assert c.stage() == "", f"исполнение не снято своим «вижу»: {c.stage()}"
    assert not c.s.reserve_code_window()["open"]
    more = c.run(RF - 20)
    assert not gw_sent(more, "qtake") and not sent(more, "ask") and not gw_sent(more, "qneed"), \
        "после своего «вижу» кандидат забирает раньше T_up_rf"
    later = c.run(40)
    assert gw_sent(later, "qtake"), "случай 3 не забран и через T_up_rf"
    assert not c.b.launched, acts


def test_own_see_in_the_points_stage_drops_the_execution_too(c):
    _taken(c)
    c.run(ASK)
    assert c.stage() == "points"
    c.own("see")
    c.tick()
    assert c.stage() == "", "сбор точек продолжается при своём «вижу»"
    c.share(1)
    assert not c.b.launched, "точка, пришедшая после снятия исполнения, открыла зону"


def test_after_the_take_my_own_probe_carries_the_next_epoch(c):
    """Исход «забираю» кандидат знает сам: его проба прежнему лидеру — под e + 1, тот
    «да» от него больше не считает."""
    _taken(c)
    assert c.v.epoch == 8, f"эпоха своего зонда после «забираю» — {c.v.epoch}, а не e + 1"


# ── исполнение: пути ────────────────────────────────────────────────────────

def test_without_the_pair_link_the_take_goes_straight_to_the_points(c):
    c.b.online = False
    acts = _taken(c)
    assert not sent(acts, "ask"), "просьба лидеру без линка пары"
    assert sorted(s for s, _ in gw_sent(acts, "qneed")) == [1, 2], "без линка пары точки не попрошены сразу"
    assert c.stage() == "points"


def test_a_take_clears_the_old_refusal_line(c):
    c.s.rsv_update("rsv_view", auto_block="готовность выключена")
    _taken(c)
    assert c.s.rsv_get("rsv_view").get("auto_block") == "", "строка прежнего отказа осталась после «забираю»"


def test_case_3_without_points_asks_them_again_instead_of_the_code(c):
    """Свой «вижу» (случай 3) — окна кода нет: точек мало — `qneed` заново."""
    c.view(1, "uns")
    c.view(2, "uns")
    c.tick()
    c.run(RF)
    c.run(ASK)
    assert c.stage() == "points"
    acts = c.run(NEED2 + 5)
    assert not c.s.reserve_code_window()["open"], "окно кода при своём «вижу»"
    assert sorted(s for s, _ in gw_sent(acts, "qneed")) == [1, 2], "точки при своём «вижу» не попрошены снова"
    assert c.stage() == "points"


# ── состав второй линии ─────────────────────────────────────────────────────

def test_a_gateway_added_to_her_line_changes_v_on_the_fly(c):
    c.tick()
    assert c.s.reserve_quorum_candidate_view()["v"] == 3
    c.s.rsv_update("rsv_peer_state", quo={"l": LINE, "g": 2, "members": MEMBERS + ["b3"], "active": True, "why": []})
    c.b.hello(3)
    c.own("uns")
    c.view(1, "uns")
    c.view(2, "uns")
    acts = c.tick()
    assert c.s.reserve_quorum_candidate_view()["v"] == 4
    assert [s for s, _ in gw_sent(acts, "qask")] == [3], "новый шлюз линии не опрошен"
    more = c.run(T_UP + 5)
    assert gw_sent(more, "qtake"), "3 из 4 «нет» — не забрано"


def test_a_malformed_line_makes_no_candidate(c):
    c.s.rsv_update("rsv_peer_state", quo={"l": LINE, "g": 1, "members": ["b", "a", "c1"], "active": True, "why": []})
    c.own("uns")
    assert c.tick() == [] and c.s.reserve_quorum_candidate_view() == {}


def test_a_line_without_this_server_makes_no_candidate(c):
    c.s.rsv_update("rsv_peer_state", quo={"l": LINE, "g": 1, "members": ["b", "b1", "b2"], "active": True, "why": []})
    c.own("uns")
    c.view(1, "uns")
    acts = c.tick() + c.run(T_UP + 5)
    assert acts == [] and c.s.reserve_quorum_candidate_view() == {}
