"""Т-РА5 и С11: слово `state{idle, reason}` резерва шлюзам основы после
срыва открытия и зона, снятая мимо бота.

- протокол: `state` с необязательным `reason` проходит словарь гостевого
  канала, без него — тоже (резерв прежней линии);
- резерв: `_take_fail` (срыв процесса открытия) шлёт всем шлюзам на связи
  `state{idle, reason}` — даже если `idle` уже уходил;
- шлюз: `idle` с причиной после отданной этим шлюзом точки ключа кворума
  (`qneed` → `qshare`) — одно уведомление админу шлюза «переезд не
  состоялся» с моментом из взгляда зонда и снятая отметка; без отданной
  точки и без причины — молчит;
- С11: зона снята мимо бота (перезагрузка резерва) — idle без паузы между
  автоматическими переездами, пакет и доля остаются: кворум открывает снова.

Резерв — `box` и кандидат кворума `Cand` (часы, шлюзы, процесс открытия подменены); шлюз —
`ag` из test_gw_quorum_decision (зонд, точка и файлы quorum.json/reserve.json — во
временном каталоге).

Цена ошибки: шлюз, отдавший долю, молчит о срыве — его админ считает, что
пользователи на резерве, а они без VPN; уведомление на каждый повтор `state` —
шум в чате шлюза; С11 с паузой или без пакета — после перезагрузки резерва
пользователи мёртвой основы остаются без сервера до прихода нового пакета,
которого мёртвая основа не пришлёт.
"""
from __future__ import annotations

import pytest

from awgbot.infra import rsvzone
from awgbot.util import pairproto
from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import T_UP, Cand, box, gw_sent, notes
from tests.unit.test_gw_quorum_decision import Q_GEN, ag, clock

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["box", "ag", "clock"]

REASON = "зона: veth не поднялся"


# ── протокол ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [{"state": "idle", "move": 3, "reason": REASON}, {"state": "serving", "move": 3}])
def test_state_with_or_without_a_reason_passes_the_guest_dictionary(body):
    assert pairproto.check(pairproto.GUEST_PROTO, "state", dict(body), pairproto.RESERVE)["state"] == body["state"]


def test_a_reason_longer_than_the_limit_is_refused_by_the_dictionary():
    with pytest.raises(pairproto.ProtocolError):
        pairproto.check(pairproto.GUEST_PROTO, "state", {"state": "idle", "move": 3, "reason": "x" * 5000},
                        pairproto.RESERVE)


# ── резерв: срыв открытия ───────────────────────────────────────────────────

@pytest.fixture()
def opening(box, monkeypatch):
    """Основа мертва: свой «не вижу» и «нет» обоих шлюзов T_up, канала пары нет —
    точки; точка шлюза 1 пришла — процесс открытия идёт."""
    monkeypatch.setattr(rsvzone, "teardown", lambda: "")
    monkeypatch.setattr(rsvzone, "wipe", lambda: "")
    monkeypatch.setattr(box.s, "_rsvopen_stop", lambda: None)
    box.primary(False)
    box.online = False
    c = Cand(box)
    c.own("uns")
    c.view(1, "uns")
    c.view(2, "uns")
    c.tick()
    c.run(T_UP + 5)
    c.share(1)
    assert box.s.reserve_in() == "taking", "предпосылка: открытие не началось"
    box.cand = c
    return box


def test_a_failed_opening_tells_every_gateway_idle_with_the_reason(opening, monkeypatch):
    monkeypatch.setattr(rsvzone, "open_status", lambda: {"step": "fail", "reason": REASON})
    acts = opening.s.reserve_in_tick()
    words = gw_sent(acts, "state")
    assert sorted(s for s, _ in words) == [1, 2], f"не всем шлюзам на связи: {words}"
    for _slot, body in words:
        assert body["state"] == "idle" and body["reason"] == REASON, body
        pairproto.check(pairproto.GUEST_PROTO, "state", dict(body), pairproto.RESERVE)


def test_the_reason_word_goes_even_if_idle_was_already_said(opening, monkeypatch):
    """force: прежний `state{idle}` без причины не гасит слово срыва."""
    opening.s.__dict__["_rsv_state_sent"] = '{"move": 0, "state": "idle"}'
    monkeypatch.setattr(rsvzone, "open_status", lambda: {"step": "fail", "reason": REASON})
    assert gw_sent(opening.s.reserve_in_tick(), "state"), "слово срыва съедено сверкой «то же состояние»"


def test_a_long_reason_is_cut_to_what_the_dictionary_takes(opening, monkeypatch):
    monkeypatch.setattr(rsvzone, "open_status", lambda: {"step": "fail", "reason": "я" * 500})
    sent = gw_sent(opening.s.reserve_in_tick(), "state")
    assert sent, "слово срыва с длинной причиной не ушло — проверять нечего"
    for _slot, body in sent:
        pairproto.check(pairproto.GUEST_PROTO, "state", dict(body), pairproto.RESERVE)


def test_the_ordinary_state_word_has_no_reason(box):
    body = box.s._state_body()
    assert body == {"state": "idle", "move": 0}, body


# ── С11: зона снята мимо бота ───────────────────────────────────────────────

@pytest.fixture()
def serving(opening, monkeypatch):
    zone = {"phase": "serving"}
    monkeypatch.setattr(rsvzone, "state", lambda *a: dict(zone))
    opening.s.reserve_set_in("serving", kind="dead", gen=3, ended_auto=True, tg_taken=True, served=True)
    assert opening.cand.tick() == [], "кандидат о линии, чьих пользователей я несу"
    opening.zone = zone
    return opening


def test_a_zone_removed_behind_the_bot_ends_without_the_pause_and_keeps_the_package(serving):
    serving.zone["phase"] = "idle"                      # резерв перезагрузился — зоны нет
    acts = serving.s.reserve_in_tick()
    inn = serving.s.rsv_get("rsv_in")
    assert inn.get("state") == "idle" and inn.get("ended_auto") is False, inn
    pkg = serving.s.rsv_get("rsv_pkg_in")
    assert pkg.get("gen") and pkg.get("share"), "пакет и доля стёрты — открыть снова нечем"
    assert [n for n in notes(acts) if "зона снята" in n.text], "админу резерва не сказано, что пользователи основы не здесь"
    assert gw_sent(acts, "state") and all(b["state"] == "idle" for _, b in gw_sent(acts, "state"))
    assert serving.s.reserve_auto_block() == "", f"матрица не вправе открыть снова: {serving.s.reserve_auto_block()}"


def test_after_the_zone_is_removed_the_candidate_takes_again_without_the_pause(serving):
    """Основа всё ещё мертва, большинство «нет» держится: после снятой зоны
    кандидат снова забирает (без 30-минутной паузы автоматических переездов)."""
    serving.zone["phase"] = "idle"
    serving.s.reserve_in_tick()
    acts = serving.cand.run(T_UP + 10)
    assert gw_sent(acts, "qtake") or gw_sent(acts, "qneed"), f"после снятой зоны переезд не начат снова: {acts}"
    assert serving.s.rsv_get("rsv_view").get("auto_block", "") == ""


def test_a_zone_that_is_still_up_changes_nothing(serving):
    assert serving.s.reserve_in_tick() == [] and serving.s.reserve_in() == "serving"


# ── шлюз: Т-РА5 ─────────────────────────────────────────────────────────────

def _moved_failed(acts) -> list:
    return [n for a in acts if a[0] == "notify" for n in a[1] if "не состоялся" in n.text]


SINCE = "2026-10-30T10:05:00+03:00"


def _gave_point(ag):
    """Зонд шлюза не видит сервер, кандидат попросил точку — отдана."""
    ag.uns()
    acts = ag.word("qneed", q_gen=Q_GEN, since=SINCE)
    assert acts and acts[0][1] == "qshare", "предпосылка: точка не отдана"


def _state(svc, **body) -> list:
    return svc.reserve_guest_in("state", {"state": "idle", "move": 3, **body})


def test_idle_with_a_reason_after_the_point_tells_the_gateway_admin_once(ag):
    """Шлюз отдал точку, процесс открытия у резерва сорвался: его админ должен
    знать, что пользователи не на резерве, — Т-РА5 один раз."""
    _gave_point(ag)
    acts = _state(ag.svc, reason=REASON)
    told = _moved_failed(acts)
    assert len(told) == 1, [n.text for a in acts if a[0] == "notify" for n in a[1]]
    assert "Сервер AWG не отвечает с" in told[0].text and "«Alpha»" in told[0].text and REASON in told[0].text, told[0].text
    assert not told[0].text.rstrip().endswith("."), "уведомление с точкой в конце"
    again = _state(ag.svc, reason=REASON)
    assert not _moved_failed(again), "Т-РА5 повторилось на тот же срыв"


def test_idle_with_a_reason_without_a_given_point_is_silent(ag):
    ag.uns()
    assert not _moved_failed(_state(ag.svc, reason=REASON)), "шлюз, не дававший точки, говорит о срыве чужого переезда"


def test_idle_without_a_reason_after_the_point_is_silent(ag):
    _gave_point(ag)
    assert not _moved_failed(_state(ag.svc))
    assert len(_moved_failed(_state(ag.svc, reason=REASON))) == 1, "обычный idle снял отметку отданной точки"


def test_serving_after_the_point_does_not_say_failed(ag):
    _gave_point(ag)
    assert not _moved_failed(ag.svc.reserve_guest_in("state", {"state": "serving", "move": 3, "reason": REASON}))


def test_the_failure_notice_survives_a_reboot_of_the_agent(ag):
    """Отметка точки — на диске: агент, перезапущенный между точкой и срывом,
    всё равно говорит админу."""
    _gave_point(ag)
    fresh = ag.new()
    assert len(_moved_failed(_state(fresh, reason=REASON))) == 1
