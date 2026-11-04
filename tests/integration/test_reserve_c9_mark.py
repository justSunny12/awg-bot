"""Отметка С9 (`rsv_c9`, journal/c9) — только там, где правки бота на резерве
придут хвостом журнала, и не дольше, чем их ждать есть смысл:

- прежний путь (пакет целиком, `_pkg_journal` ложно): «вернуть без резерва» и
  снятие вебхука отметку не заводят — правки гостя вольются репликой;
- срыв переезда после передачи бота (`_move_fail`, шаг released) и снятый
  зависший вебхук без рычага (`reserve_stuck_home`) на журнале: эпоха здесь
  без released, гость за мёртвым линком мог жить — отметка стоит, пока резерв
  живым словом после перехода домой не скажет, что гостя нет (и его журнал на
  диске не новее перенесённого); тогда такт дома (`reserve_c9_settle`) её снимает;
- с рычагом (await_merge) такт дома её не трогает — ждёт возврата;
- released при держателе дома сверяет эпоху: отметка другой эпохи (прошлый
  эпизод) — устарела, снимается, недостающее по её `upto` не просится.

Цена ошибки: висящая отметка перехватывает события эпохи гостя как «хвост С9»
вместо jstale, держит в `reserve_disk_waiting` ожидание чужого журнала и
приносит Т-Ж4/Т-Ж5 с устаревшими цифрами при следующем возврате, через
переезды; заведённая на прежнем пути — то же без единой записи журнала;
снятая по одной смене состояния — правки людей у живого гостя за мёртвым
линком теряются молча; устаревшая отметка при released — jneed хвоста, которого
у гостя нет, возврат ждёт срок шага и пишет «не применено» о несуществующем.
"""
from __future__ import annotations

import pytest

from awgbot.infra import awg
from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_jrn_duo import JCAPS, Duo, sends

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


# ── released при держателе дома: отметка — только своей эпохи ───────────────

def _returning_holder_with_c9(duo) -> dict:
    """«Вернуть без резерва» на журнале (эпоха здесь, отметка С9 эпохи гостя), резерв вышел на
    связь с гостем — возврат с шага back_req, право записи остаётся здесь (как _tick_await_merge)."""
    _following_by_journal(duo)
    _gone(duo.a)
    duo.a.reserve_force_home()
    duo.a.reserve_stuck_home()
    c9 = duo.a.rsv_get("rsv_c9")
    assert c9.get("e"), "условие: отметка С9 заведена"
    duo.a.reserve_set_out("returning", kind="dead", gen=7, return_why="force", await_merge=False, step="back_req")
    duo.a.reserve_epoch_sync()
    m = duo.a.db.jrn_meta()
    assert (m["holder"], m["role"]) == ("self", "holder"), f"условие: на back_req после рычага держатель здесь: {m}"
    return c9


def test_a_released_of_the_c9_epoch_still_asks_for_the_missing_edits(duo):
    """Отметка той же эпохи, что released, — недостающее после перенесённого просим (jneed)."""
    c9 = _returning_holder_with_c9(duo)
    acts = duo.a.reserve_released_jrn_in(int(c9["e"]), int(c9["upto"]) + 3)
    assert [b["from"] for _, b in sends(acts, "jneed")] == [int(c9["upto"]) + 1], acts
    assert duo.a.rsv_get("rsv_c9").get("e") == c9["e"], "отметку своей эпохи сняли"


def test_a_released_of_another_epoch_drops_the_stale_c9_and_takes_the_epoch(duo):
    """Отметка прошлого эпизода (не снята фактом) при released другой эпохи: её upto — не о
    номерах этого гостя; jneed по ней просил бы хвост, которого нет, а возврат ждал бы срок шага."""
    c9 = _returning_holder_with_c9(duo)
    duo.a.rsv_update("rsv_c9", e=int(c9["e"]) - 1)        # отметка прежнего эпизода
    acts = duo.a.reserve_released_jrn_in(int(c9["e"]), int(c9["upto"]) + 3)
    assert not sends(acts, "jneed"), f"устаревшая отметка С9 запросила хвост, которого нет: {acts}"
    assert ("take_tg",) in acts, acts
    assert duo.a.rsv_get("rsv_c9") == {}, "устаревшая отметка С9 осталась — Т-Ж4/Т-Ж5 с чужими цифрами"


# ── срыв после передачи бота при живом госте за разрывом ─────────────────────

def test_back_at_the_live_guest_after_a_failed_move_brings_its_edits_as_the_c9_tail_and_drops_the_mark(duo, monkeypatch, caplog):
    """Переезд кнопкой: гость взял эпоху по released, линк пары порвался, основа по сроку и
    слову Telegram сорвалась домой (эпоха здесь, отметка С9 под эпоху гостя); гость за
    разрывом правит. Линк вернулся — основа повторяет `back` (он ушёл в мёртвый канал);
    резерв гостя не убивает, а останавливает: его правки доходят хвостом С9, по его released
    зона снимается, и живой st резерва без гостя снимает отметку фактом с числом перенесённого."""
    import logging
    import shutil
    from awgbot.infra import rsvzone
    calls: list[str] = []
    monkeypatch.setattr(rsvzone, "teardown", lambda: calls.append("teardown") or "")
    monkeypatch.setattr(rsvzone, "relay", lambda to: calls.append("relay") or "")
    monkeypatch.setattr(rsvzone, "wipe", lambda: (calls.append("wipe"), shutil.rmtree(rsvzone.VAR_DIR / "jrn", ignore_errors=True)) and "")
    duo.journal_on()
    duo.edit("Иван")
    duo.tick()
    duo.open_guest(button=True)
    rm.to_step(duo.a, "release")
    built = duo.a.reserve_release_build()
    assert built is not None and duo.a.rsv_get("rsv_out")["step"] == "released" and duo.a.reserve_following()
    duo.b_to_guest(duo.b.reserve_released_jrn_store(built["epoch"], built["last"]))
    with duo.as_guest():
        gm = duo.gdb.jrn_meta()
        assert (gm["holder"], gm["role"]) == ("self", "holder"), gm
    duo.b.reserve_guest_jrn_seen({"e": gm["epoch"], "fork": gm["fork_i"], "last": duo.gdb.jrn_last_i()})
    # линк порван: срыв по сроку шага и слову Telegram — дома держатель, back ушёл в мёртвый канал
    acts = duo.a._move_fail("резерв не взял право отвечать — вебхук не поставлен", released=True)
    assert "back" in rm.sent(acts) and duo.a.reserve_out() == "home"
    duo.a.reserve_out_tick()
    c9 = duo.a.rsv_get("rsv_c9")
    assert c9.get("e") == gm["epoch"], c9
    with duo.as_guest():
        duo.g.db.create_client("Ольга", 1, "2026-01-01T00:00:00+03:00", "2099-01-01T00:00:00+03:00", "INV-DUMMY-O")   # правка гостя за разрывом
    # линк вернулся: st резерва — всё ещё taking под мой переезд → повтор back
    rm.st_in(duo.a, caps=JCAPS, jrn={"keep": duo.b.reserve_keep_view()},
             **{"in": "taking", "in_by": "k", "in_kind": "button", "pkg": {"gen": built["gen"], "fp": ""}})
    acts = duo.a.reserve_out_tick()
    assert rm.sent(acts) == ["back"], f"back после срыва не повторён: {acts}"
    at_b = duo.b.reserve_back_in()
    assert at_b == [("guest", "stop", {})], f"back при живом госте снял зону, не остановив его: {at_b}"
    assert calls == [], calls
    # гость останавливается: последние записи — основе (хвост С9), подтверждение — ему, released
    with duo.as_guest():
        msgs = duo.g.guest_jrn_tick()
    at_a = duo.b_to_a(duo.guest_to_b(msgs))
    assert "Ольга" in {c.name for c in duo.a.db.list_clients()}, "правка гостя не перенесена хвостом С9"
    duo.b_to_guest(duo.a_to_b(at_a))
    with duo.as_guest():
        assert duo.g.guest_jrn_drained(), "основа не подтвердила записи гостя"
        rel = duo.g.guest_jrn_release()
    done = duo.b.reserve_guest_released_jrn(rel)
    assert not [a for a in done if a[0] == "send_released"], done
    assert [a[1] for a in done if a[0] == "send"] == ["relay", "wiped"] and calls[-1] == "wipe", (done, calls)
    assert duo.b.reserve_in() == "relay"
    # живой st резерва без гостя после моего перехода домой — отметка снята фактом
    duo.b.__dict__.pop("_rsv_guest_jrn", None)
    with caplog.at_level(logging.INFO):
        rm.st_in(duo.a, caps=JCAPS, jrn={"keep": duo.b.reserve_keep_view()}, **{"in": "relay"})
        notes = duo.a.reserve_out_tick()
    assert duo.a.rsv_get("rsv_c9") == {}, "отметка С9 стоит, хотя гостя у резерва больше нет"
    lines = [r.getMessage() for r in caplog.records if "отметка С9 снята (перенесено " in r.getMessage()]
    assert lines and "(перенесено 0)" not in lines[0], lines   # число — событий, не профилей (стенд ждёт эту строку)
    texts = [n.text for n in rm.notices(notes)]
    assert any("после потери связи перенесены сюда: " in t for t in texts), texts
