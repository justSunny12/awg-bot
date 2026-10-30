"""Слово Telegram «вебхук у резерва» дома у линии с кворумом — полный круг
(domain/services/reserve/outbound.py `_tick_home`/`_tick_away`, journal/epoch.py;
решение 31.10.2026, вариант Б): 409 дома — следование и away (`by = webhook`) с
отметками home в записи away; живой резерв, сказавший после перехода, что моих
не несёт, — остаток вебхука: домой с прежними отметками, запись здесь, без
дребезга home ↔ away; away по принятой эпохе (`by = share`) так не возвращается.

Мир — основа «Alpha» дома в паре с «Beta» (tests/integration/_reserve_move.py),
своя линия `a` с кворумом (`_Q` из test_reserve_quorum_cov_leader.py: включённость
— подмена предиката, пробы — настоящие датаграммы, часы лидера — модельные).

Цена ошибки: остаток вебхука держит основу в away — бот молчит своим
пользователям, хотя они дома, а возврат «сразу» ждёт голосов, которые и так
есть; возврат домой с новыми отметками — проверка зависшего вебхука и роль эпохи
считают от момента возврата, и вебхук резерва снимается на минуты позже; дребезг
home ↔ away — на каждом такте живое сообщение и смена права записи.
"""
from __future__ import annotations

import pytest

from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration.test_reserve_quorum_cov_leader import _Q, _lead

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv


@pytest.fixture()
def q(mv, monkeypatch):
    w = _Q(mv.svc, monkeypatch)
    _lead(w)
    return w


def _silent_peer(q) -> None:
    q.s.rsv_update("rsv_peer_state", st_stale=True)


def _live_idle_peer(q, monkeypatch, inn: str = "idle", ahead: int = 3600) -> None:
    monkeypatch.setattr(q.s, "reserve_peer_seen", lambda: "ok")
    q.s.rsv_update("rsv_peer_state", session=True, inn=inn, st_stale=False,
                   at=timeutil.to_iso(timeutil.shift(timeutil.now(), ahead)))


def _webhook_away(q) -> dict:
    """Дома: поллинг получил 409 — автомат уходит в away по слову Telegram."""
    home = dict(q.s.rsv_get("rsv_out"))
    _silent_peer(q)
    q.s.reserve_set_poll("away", why="webhook")
    q.s.reserve_out_tick()
    out = q.s.rsv_get("rsv_out")
    assert out.get("state") == "away" and out.get("by") == "webhook", f"подготовка: 409 не повёл в away: {out}"
    return home


def test_the_away_record_keeps_the_home_marks(q):
    home = _webhook_away(q)
    out = q.s.rsv_get("rsv_out")
    assert out.get("home_since") == str(home.get("since") or ""), "отметка home не сохранена в записи away"
    assert out.get("home_at_ts") == home.get("at_ts"), "доли секунды отметки home не сохранены"
    assert q.s.db.jrn_following(), "в away по 409 основа пишет решения"


def test_a_live_reserve_saying_idle_after_the_move_brings_me_home_with_the_old_marks(q, monkeypatch):
    home = _webhook_away(q)
    _live_idle_peer(q, monkeypatch)
    acts = q.s.reserve_out_tick()
    out = q.s.rsv_get("rsv_out")
    assert out.get("state") == "home", f"остаток вебхука оставил основу в away: {out}"
    assert out.get("since") == str(home.get("since") or "") and out.get("at_ts") == home.get("at_ts"), \
        f"возврат домой не с прежними отметками: {out} против {home}"
    assert "home_since" not in out and "home_at_ts" not in out and not out.get("by"), out
    assert ("live",) in acts
    assert not q.s.db.jrn_following(), "дома после остатка вебхука основа не пишет решения"


def test_after_coming_home_from_a_remainder_the_automaton_does_not_flap(q, monkeypatch):
    _webhook_away(q)
    _live_idle_peer(q, monkeypatch)
    q.s.reserve_out_tick()
    for _ in range(5):
        q.s.reserve_out_tick()
        assert q.s.reserve_out() == "home", "остаток вебхука снова повёл основу в away — дребезг"


def test_a_remainder_word_does_not_lead_away_again_when_the_session_drops(q, monkeypatch):
    """Дома после остатка вебхука сессия пары рвётся: то же слово Telegram (409) второй раз в away
    не ведёт — одно слово, одно решение; новое 409 после возврата права — ведёт."""
    _webhook_away(q)
    _live_idle_peer(q, monkeypatch)
    q.s.reserve_out_tick()
    assert q.s.reserve_out() == "home"
    _silent_peer(q)
    monkeypatch.setattr(q.s, "reserve_peer_seen", lambda: "down")
    for _ in range(3):
        q.s.reserve_out_tick()
        assert q.s.reserve_out() == "home", "обрыв сессии после остатка вебхука снова увёл основу в away — дребезг"
    assert q.s._follow_wanted() == (False, "вебхук у резерва без гостя"), "роль следует за словом, уже признанным остатком"
    q.s.reserve_set_poll("mine")
    q.s.reserve_set_poll("away", why="webhook")               # новое слово — новая отметка
    q.s.reserve_out_tick()
    assert q.s.reserve_out() == "away", "новое 409 после возврата права не повело в away"


def test_a_reserve_still_serving_keeps_me_away(q, monkeypatch):
    _webhook_away(q)
    _live_idle_peer(q, monkeypatch, inn="serving")
    q.s.reserve_out_tick()
    assert q.s.reserve_out() == "away", "резерв несёт моих пользователей, а основа вернулась домой"


def test_an_idle_word_from_before_the_move_is_not_a_remainder(q, monkeypatch):
    _webhook_away(q)
    _live_idle_peer(q, monkeypatch, ahead=-3600)
    q.s.reserve_out_tick()
    assert q.s.reserve_out() == "away", "слово резерва, сказанное до перехода, вернуло основу домой"


def test_a_silent_reserve_keeps_me_away(q):
    _webhook_away(q)
    q.s.reserve_out_tick()
    assert q.s.reserve_out() == "away"


def test_an_away_by_the_adopted_epoch_is_not_undone_by_an_idle_word(q, monkeypatch):
    """В away по принятой эпохе кворума (`by = share`) — не слово Telegram: возвращает
    правило лидера по факту второй стороны, а не ветка остатка вебхука."""
    e = q.s.reserve_quorum_epoch()
    q.t += 61
    q.tick()
    q.t += 20
    q.tick()
    q.probe("a1", e=e + 1)
    monkeypatch.setattr(q.s, "reserve_peer_seen", lambda: "down")
    q.s.reserve_out_tick()
    out = q.s.rsv_get("rsv_out")
    assert out.get("state") == "away" and out.get("by") == "share", out
    _live_idle_peer(q, monkeypatch)
    q.s.reserve_out_tick()
    assert q.s.rsv_get("rsv_out").get("by") == "share" and q.s.reserve_out() == "away", \
        "away по принятой эпохе снят веткой остатка вебхука"
