"""Отметка С9 (`rsv_c9`, journal/c9) — только там, где правки бота на резерве
придут хвостом журнала, и не дольше, чем их ждать есть смысл:

- прежний путь (пакет целиком, `_pkg_journal` ложно): «вернуть без резерва» и
  снятие вебхука отметку не заводят — правки гостя вольются репликой;
- срыв переезда после передачи бота (`_move_fail`, шаг released) и снятый
  зависший вебхук без рычага (`reserve_stuck_home`) на журнале: эпоха здесь
  без released, гость за мёртвым линком мог жить — отметка стоит, пока резерв
  живым словом после перехода домой не скажет, что гостя нет (и его журнал на
  диске не новее перенесённого); тогда такт дома (`reserve_c9_settle`) её снимает;
- с рычагом (await_merge) такт дома её не трогает — ждёт возврата.

Цена ошибки: висящая отметка перехватывает события эпохи гостя как «хвост С9»
вместо jstale, держит в `reserve_disk_waiting` ожидание чужого журнала и
приносит Т-Ж4/Т-Ж5 с устаревшими цифрами при следующем возврате, через
переезды; заведённая на прежнем пути — то же без единой записи журнала;
снятая по одной смене состояния — правки людей у живого гостя за мёртвым
линком теряются молча.
"""
from __future__ import annotations

import pytest

from awgbot.infra import awg
from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_jrn_duo import JCAPS, Duo

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv


def _ago(minutes: float) -> str:
    return timeutil.to_iso(timeutil.shift(timeutil.now(), -minutes * 60))


def _gone(svc) -> None:
    """Мои пользователи у резерва, он не на связи 11 минут, запись — ботом."""
    rm.go_away(svc, kind="dead")
    rm.automatic(svc)
    svc.db.set_state("last_server_ok", "0")
    svc.rsv_update("rsv_peer_state", session=False, down_since=_ago(11))


@pytest.fixture()
def duo(mv, tmp_path, monkeypatch, fake_awg):
    monkeypatch.setattr(awg, "blocked_ips", lambda: set(fake_awg.blocked), raising=False)
    d = Duo(mv, tmp_path, monkeypatch)
    d.fake = fake_awg
    yield d
    d.close()


# ── прежний путь ────────────────────────────────────────────────────────────

def test_the_lever_on_the_package_path_starts_no_c9_mark(mv):
    svc = mv.svc
    _gone(svc)
    assert svc.reserve_following() and not svc._pkg_journal(), "условие: следование прежним путём"
    svc.reserve_force_home()
    assert svc.rsv_get("rsv_c9") == {}, "прежний путь завёл отметку С9 — хвоста журнала здесь не будет"
    svc.reserve_stuck_home()
    m = svc.db.jrn_meta()
    assert (m["holder"], m["role"]) == ("self", "holder"), m
    assert svc.rsv_get("rsv_c9") == {}, "снятие вебхука по рычагу завело отметку С9 на прежнем пути"
    assert svc.rsv_get("rsv_out").get("await_merge") is True, "рычаг без ожидания реплики"


def test_a_stuck_webhook_on_the_package_path_starts_no_c9_mark(mv):
    svc = mv.svc
    rm.go_away(svc, kind="dead")
    assert svc.reserve_following()
    svc.reserve_stuck_home()
    assert svc.reserve_out() == "home" and not svc.reserve_following()
    assert svc.rsv_get("rsv_c9") == {}


# ── журнал ──────────────────────────────────────────────────────────────────

def _following_by_journal(duo) -> None:
    duo.journal_on()
    duo.edit("Иван")
    duo.tick()
    duo.open_guest(button=False)
    rm.go_away(duo.a, kind="dead")
    duo.a.reserve_epoch_sync()
    assert duo.a.reserve_following() and duo.a._pkg_journal(), "условие: следование журналом"


def _idle_after_home(duo) -> None:
    """Резерв снова на связи живым словом после моего перехода домой: гостя нет."""
    rm.st_in(duo.a, caps=JCAPS, jrn={"keep": duo.b.reserve_keep_view()}, **{"in": "idle"})


def test_a_failed_move_after_the_bot_was_handed_over_keeps_c9_until_the_reserve_says_idle(duo):
    duo.journal_on()
    duo.edit("Иван")
    duo.tick()
    assert duo.a.reserve_jrn_moved(), "условие: у резерва снимок журнала"
    rm.to_step(duo.a, "release")
    assert duo.a.reserve_release_build() is not None
    assert duo.a.rsv_get("rsv_out")["step"] == "released" and duo.a.reserve_following(), \
        "передача бота не увела эпоху — сценарий не о том"
    duo.a._move_fail("резерв не взял право отвечать — вебхук не поставлен", released=True)
    m = duo.a.db.jrn_meta()
    assert duo.a.reserve_out() == "home" and (m["holder"], m["role"]) == ("self", "holder"), m
    duo.a.reserve_out_tick()
    assert duo.a.rsv_get("rsv_c9").get("e"), "отметка снята без слова резерва — правки живого гостя потеряются"
    _idle_after_home(duo)
    duo.a.reserve_out_tick()
    assert duo.a.reserve_out() == "home"
    assert duo.a.rsv_get("rsv_c9") == {}, "срыв переезда оставил отметку С9 — ждём правок, которых не будет"


def test_a_stuck_webhook_taken_without_the_lever_drops_c9_once_the_reserve_says_idle(duo):
    _following_by_journal(duo)
    duo.a.reserve_stuck_home()
    m = duo.a.db.jrn_meta()
    assert duo.a.reserve_out() == "home" and (m["holder"], m["role"]) == ("self", "holder"), m
    assert duo.a.rsv_get("rsv_c9").get("e"), "условие: эпоха взята без released — отметка заведена"
    _idle_after_home(duo)
    duo.a.reserve_out_tick()
    assert duo.a.rsv_get("rsv_c9") == {}, "снятый вебхук без рычага оставил отметку С9"


def test_an_idle_word_said_before_going_home_does_not_drop_c9(duo):
    """`st` «idle», полученный до перехода домой, — не факт: гость мог открыться после."""
    _following_by_journal(duo)
    _idle_after_home(duo)
    duo.a.reserve_stuck_home()
    duo.a.reserve_out_tick()
    assert duo.a.rsv_get("rsv_c9").get("e"), "старое слово резерва сняло отметку"


def test_the_guest_journal_on_the_reserve_disk_keeps_c9(duo):
    _following_by_journal(duo)
    duo.a.reserve_stuck_home()
    c9 = duo.a.rsv_get("rsv_c9")
    keep = dict(duo.b.reserve_keep_view(), gj_e=c9["e"], gj_last=int(c9["upto"]) + 3)
    rm.st_in(duo.a, caps=JCAPS, jrn={"keep": keep}, **{"in": "idle"})
    duo.a.reserve_out_tick()
    assert duo.a.rsv_get("rsv_c9").get("e"), "журнал гостя на диске резерва новее — а отметку сняли"


def test_the_lever_with_the_journal_keeps_the_c9_mark_until_the_return(duo):
    _following_by_journal(duo)
    guest_e = duo.a.db.jrn_meta()["epoch"]
    _gone(duo.a)
    duo.a.reserve_force_home()
    duo.a.reserve_stuck_home()
    c9 = duo.a.rsv_get("rsv_c9")
    assert c9.get("e") == guest_e, f"рычаг с журналом без отметки С9 — правки бота на резерве потеряются: {c9}"
