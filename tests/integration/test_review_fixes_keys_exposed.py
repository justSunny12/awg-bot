"""Пакет открывали без моего K — новое поколение ключа на любом пути домой и дома
(domain/services/reserve/keys.py `reserve_keys_after_foreign_open`, `reserve_exposed_in`,
`reserve_compromised_settle`, `reserve_exposed_mark`; outbound_return `_forced_no_merge`,
`_return_done`; outbound_stuck `reserve_stuck_home`; outbound `_tick_away` — остаток
вебхука; inbound `_tick_taking`, `_take_fail`; opening `reserve_open_by_points`; поле
`st.exposed`).

Сторона резерва: точки ключа кворума у меня (собраны для открытия, запущен процесс или
нет) — отметка `exposed.share` по поколению `M` пакета; код подошёл (процесс прошёл шаг
`m`, или отказ после него) — `exposed.code`; неверный код — отметки нет. Отметка уходит
основе полем `st.exposed`.

Сторона основы: `st.exposed` о текущем поколении — `compromised`; дома без хода и без моих
пользователей у резерва — новое поколение `M` и `K` сразу (по коду — и новый код, Т-РУ14),
иначе — на пути домой. Пути домой без возврата («вернуть без резерва», резерв вернулся
пустым; зависший вебхук из away; остаток вебхука) делают то же, что `_return_done`: без
моего `go` — новое поколение и Т-РУ17. Один раз на эпизод.

Цена ошибки: резерв, собравший `M` по точкам или по коду, а потом сорвавший открытие (или
вернувший пользователей обходным путём), читает все следующие пакеты под тем же `M`/`K`, а
под прежним `Mc` — и все будущие ящики кода; неверный код, сменивший код основы, — админ
повторяет верный, а он уже не подходит; двойная смена поколения на один эпизод — лишний
пакет и окно без ящиков у резерва.
"""
from __future__ import annotations

import pytest

from awgbot.util import pairproto, timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import notices
from tests.integration.test_reserve_hosting import host

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["host"]


def _gens(svc) -> tuple[int, int]:
    return int(svc.reserve_keys().get("m_gen") or 0), int(svc.reserve_qkeys().get("c_gen") or 0)


def _away_by_points(svc) -> None:
    """Мои пользователи у резерва по точкам (мимо меня: `go` не отдавал)."""
    rm.go_away(svc, kind="dead")
    svc.rsv_update("rsv_out", go_sent=False, by="share")


def _texts(acts) -> list[str]:
    return [n.text for n in notices(acts)]


# ── пути домой без возврата ─────────────────────────────────────────────────

def test_forced_home_with_an_empty_reserve_after_a_move_by_points_makes_a_new_generation(mv):
    """«Вернуть без резерва» (аварийно, по кворуму), резерв вышел на связь пустым: реплики нет —
    а `M` он знал: новое поколение, код прежний, Т-РУ17 (токен DNS был у него)."""
    svc = mv.svc
    rm.automatic(svc)
    mv.rsv.gw = False
    _away_by_points(svc)
    m0, c0 = _gens(svc)
    code0 = svc.reserve_code()
    svc.rsv_update("rsv_peer_state", session=False, down_since="2026-01-01T00:00:00+00:00")
    svc.reserve_force_home(check=False)
    svc.reserve_stuck_home()
    rm.st_in(svc, **{"in": "idle"})
    acts = svc.reserve_out_tick()
    assert svc.reserve_out() == "home"
    assert _gens(svc) == (m0 + 1, c0), f"резерв знал M, а поколение прежнее: {_gens(svc)}"
    assert svc.reserve_code() == code0 and not svc.reserve_code_notify_pending(), "по точкам код сменился"
    assert any("Токен API DNS" in t for t in _texts(acts)), _texts(acts)
    svc.reserve_out_tick()
    rm.st_in(svc, **{"in": "idle"})
    assert _gens(svc)[0] == m0 + 1, "поколение сменилось второй раз за тот же эпизод"


def test_forced_home_after_a_move_by_code_changes_the_code_too(mv):
    svc = mv.svc
    mv.rsv.gw = False
    rm.go_away(svc, kind="code")
    svc.rsv_update("rsv_out", go_sent=False, by="share")
    m0, c0 = _gens(svc)
    svc.rsv_update("rsv_peer_state", session=False, down_since="2026-01-01T00:00:00+00:00")
    svc.reserve_force_home(check=False)
    svc.reserve_stuck_home()
    rm.st_in(svc, **{"in": "idle"})
    svc.reserve_out_tick()
    assert _gens(svc) == (m0 + 1, c0 + 1), f"после переезда по коду код прежний: {_gens(svc)}"
    assert svc.reserve_code_notify_pending(), "новый код админу не поставлен"


def test_forced_home_after_a_button_move_keeps_the_generation(mv):
    """`go` отдавал я (K конвертом) — прямая цела: только новый K."""
    svc = mv.svc
    mv.rsv.gw = False
    rm.go_away(svc, kind="button")
    svc.rsv_update("rsv_out", go_sent=True)
    m0, c0 = _gens(svc)
    k0 = svc.reserve_k()
    svc.rsv_update("rsv_peer_state", session=False, down_since="2026-01-01T00:00:00+00:00")
    svc.reserve_force_home(check=False)
    svc.reserve_stuck_home()
    rm.st_in(svc, **{"in": "idle"})
    svc.reserve_out_tick()
    assert _gens(svc) == (m0, c0) and svc.reserve_k() != k0


def test_a_stuck_webhook_from_away_after_a_move_by_points_makes_a_new_generation(mv):
    svc = mv.svc
    _away_by_points(svc)
    m0, c0 = _gens(svc)
    svc.reserve_stuck_home()
    assert svc.reserve_out() == "home" and _gens(svc) == (m0 + 1, c0)


def test_a_stuck_webhook_at_home_without_a_mark_keeps_the_generation(mv):
    """Дома (основа следовала по слову Telegram): отметки нет — пакет моим K не открывали."""
    svc = mv.svc
    svc.rsv_update("rsv_out", go_sent=False, by="share")       # остаток прошлого эпизода в rsv_out
    m0 = _gens(svc)[0]
    svc.reserve_stuck_home()
    assert _gens(svc)[0] == m0


def test_a_webhook_remainder_with_the_mark_makes_a_new_generation(mv, monkeypatch):
    """В away по одному слову Telegram, живой резерв моих не несёт, но сказал, что открывал
    пакет без моего K (точки у него): домой — с новым поколением."""
    svc = mv.svc
    rm.go_away(svc, kind="dead")
    svc.rsv_update("rsv_out", go_sent=False, by="webhook", home_since=timeutil.now_iso())
    m0 = _gens(svc)[0]
    rm.st_in(svc, **{"in": "idle", "exposed": {"share": m0}})
    assert svc.reserve_compromised() and _gens(svc)[0] == m0, "в away поколение сменилось до пути домой"
    monkeypatch.setattr(svc, "_webhook_remainder", lambda out, peer: True)
    svc.reserve_out_tick()
    assert svc.reserve_out() == "home" and _gens(svc)[0] == m0 + 1, f"остаток вебхука: поколение {_gens(svc)}"
    assert not svc.reserve_compromised()


# ── дома: сорванное открытие после сборки ключа ─────────────────────────────

def test_at_home_the_word_about_points_makes_a_new_generation_at_once_and_once(mv):
    svc = mv.svc
    m0, c0 = _gens(svc)
    k0 = svc.reserve_k()
    rm.st_in(svc, **{"in": "idle", "exposed": {"share": m0}})
    assert _gens(svc) == (m0 + 1, c0) and svc.reserve_k() != k0, f"дома после открытия по точкам: {_gens(svc)}"
    assert not svc.reserve_compromised()
    rm.st_in(svc, **{"in": "idle", "exposed": {"share": m0}})     # то же слово о прежнем поколении
    assert _gens(svc)[0] == m0 + 1, "одно слово резерва сменило поколение дважды"


def test_at_home_the_word_about_the_code_changes_the_code(mv):
    svc = mv.svc
    m0, c0 = _gens(svc)
    code0 = svc.reserve_code()
    rm.st_in(svc, **{"in": "idle", "exposed": {"code": m0}})
    assert _gens(svc) == (m0 + 1, c0 + 1) and svc.reserve_code() != code0
    assert svc.reserve_code_notify_pending(), "новый код админу не поставлен"


def test_a_word_about_an_older_generation_changes_nothing(mv):
    svc = mv.svc
    svc.reserve_new_generation("подготовка", code=False)
    m0, c0 = _gens(svc)
    rm.st_in(svc, **{"in": "idle", "exposed": {"share": m0 - 1, "code": m0 - 1}})
    assert _gens(svc) == (m0, c0) and not svc.reserve_compromised()


def test_while_the_reserve_holds_my_users_the_mark_waits_for_the_return_and_rotates_once(mv):
    """Резерв несёт моих (serving): поколение не меняется посреди эпизода — только отметка;
    возврат делает одно новое поколение, не два."""
    svc = mv.svc
    m0 = _gens(svc)[0]
    rm.st_in(svc, **{"in": "serving", "exposed": {"share": m0}})
    assert svc.reserve_compromised() and _gens(svc)[0] == m0, "поколение сменилось, пока резерв несёт моих"
    svc.reserve_set_out("returning", kind="dead", gen=7, go_sent=False, return_started_at=timeutil.now_iso())
    svc._out_step("relay", 300)
    svc.reserve_wiped_in()
    assert _gens(svc)[0] == m0 + 1 and not svc.reserve_compromised()


def test_a_button_return_with_the_mark_rotates(mv):
    """`go` отдавал я, но резерв сказал, что точки у него были (прежнее открытие сорвалось, пока
    он нёс…) — отметка сильнее своей: новое поколение после возврата."""
    svc = mv.svc
    m0 = _gens(svc)[0]
    svc.reserve_mark_compromised("проверка", by="share")
    svc.reserve_set_out("returning", kind="button", gen=7, go_sent=True, return_started_at=timeutil.now_iso())
    svc._out_step("relay", 300)
    svc.reserve_wiped_in()
    assert _gens(svc)[0] == m0 + 1


def test_st_carries_the_exposed_field_through_the_pair_protocol(mv):
    body = mv.svc.reserve_st_body()
    assert "exposed" not in body, "без отметки поле не нужно"
    mv.svc.rsv_put("rsv_pkg_in", {"gen": 7, "fp": "DUMMY-fp", "m_gen": 3})
    mv.svc.reserve_exposed_mark("share")
    body = mv.svc.reserve_st_body()
    assert body["exposed"] == {"share": 3}
    assert pairproto.check(pairproto.PAIR_PROTO, "st", dict(body), pairproto.INVITER)["exposed"] == {"share": 3}


# ── сторона резерва ─────────────────────────────────────────────────────────

def test_points_in_hand_are_marked_even_if_the_opening_is_refused(host):
    svc = host.svc
    svc.reserve_set_in("serving")                               # зона занята — процесс не запустится
    svc.reserve_open_by_points({"qpoints": [[1, "00" * 16], [2, "11" * 16]], "qbox": "00" * 56}, "dead", "q")
    assert svc.reserve_st_body().get("exposed") == {"share": 1}


def _code_taking(host) -> None:
    host.svc.reserve_set_in("taking", kind="code", by="code", gen=7, open_started=timeutil.now_iso(), deadline=10 ** 12)


def test_a_code_that_assembled_the_key_is_marked(host):
    _code_taking(host)
    host.zone.status = {"step": "m"}
    host.svc.reserve_in_tick()
    assert host.svc.reserve_st_body().get("exposed") == {"code": 1}


@pytest.mark.parametrize("reason, marked", [("аварийный код: ящик не открылся", False),
                                            ("зона: nft не принял правила", True),
                                            ("ключ пакета: не тот", True)])
def test_a_failed_code_opening_is_marked_only_after_the_key(host, reason, marked):
    _code_taking(host)
    host.zone.status = {"step": "fail", "reason": reason}
    host.svc.reserve_in_tick()
    assert host.svc.reserve_in() == "idle"
    assert (host.svc.reserve_st_body().get("exposed") == {"code": 1}) is marked, reason


def test_a_button_opening_is_never_marked(host):
    host.svc.reserve_set_in("taking", kind="button", by="k", gen=7, open_started=timeutil.now_iso(), deadline=10 ** 12)
    host.zone.status = {"step": "clone_up"}
    host.svc.reserve_in_tick()
    host.zone.status = {"step": "fail", "reason": "зона: отказ"}
    host.svc.reserve_in_tick()
    assert "exposed" not in host.svc.reserve_st_body()
