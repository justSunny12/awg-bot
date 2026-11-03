"""Журнал гостя после передачи (С9) доставляет резерв
(journal/c9deliver), основа только ждёт (journal/c9, c9wait) — мир
tests/integration/_reserve_jrn_duo.py:

- основа взяла эпоху без released и без рычага (снятый зависший вебхук) — в
  `st.jrn.mine.c9` говорит, какие правки ждёт (эпоха гостя, `upto`); только
  держателем;
- гостя у резерва нет, а его журнал на диске есть — резерв сам шлёт записи этой
  эпохи подряд после `upto`; без подтверждения — повторы с нарастающей паузой
  (± разброс) до C9_WAIT_S, дальше — раз в C9_RETRY_SLOW_S, не бросая; `jack`
  основы — следующая часть сразу; `jneed` основы — из журнала гостя, а не из
  хранилища её событий;
- всё передано или журнала нет (в том числе после перезапуска резерва — по
  первому свежему `st` основы) — слово `jc9none{e, last}`; основа по нему снимает
  отметку фактом и шлёт итог Т-Ж5; другой эпохи, под рычагом или с `last` дальше
  перенесённого — не снимает; гость поднят — резерв молчит (шлёт гость);
  каталог журнала не читается — не «журнала нет»;
- карточка основы: «не ждать?» — когда за C9_WAIT_S от заведения не пришли ни
  журнал, ни «журнала нет»; пока части приходят (за C9_PROGRESS_S) — ссылки нет,
  «🗑 Не ждать» отказывает «приходит — подожди».

Ключи — заглушки (`DUMMY`), K — настоящие 32 байта.

Цена ошибки: резерв, который сам не шлёт журнал гостя, оставляет основу с
отметкой, ждущей то, что никто не принесёт, — правки людей на резерве лежат на
его диске, пока админ не нажмёт «не ждать» и не потеряет их; повтор без паузы
или строго периодический — шум и маячок в канале; брошенный после срока —
та же потеря при долгом обрыве; «журнала нет», сказанное при недоставленном
журнале или при нечитаемом каталоге, — отметка снята, правки потеряны молча;
отметка, которую слово снимает под рычагом или чужой эпохи, — возврат без хвоста;
ссылка «не ждать» при идущем журнале — админ сам обрывает доставку.
"""
from __future__ import annotations

import types

import pytest

from awgbot.bot.texts.reserve import section as st_t
from awgbot.domain.notices import reserve_tail as rt
from awgbot.domain.services import ServiceError
from awgbot.domain.services.reserve.journal import c9deliver
from awgbot.domain.services.reserve.journal.c9deliver import C9_RETRY_FIRST_S, C9_RETRY_SLOW_S
from awgbot.domain.services.reserve.journal.c9wait import C9_PROGRESS_S, C9_WAIT_S
from awgbot.infra import awg, rsvzone
from awgbot.util import backoff, timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_jrn_duo import JCAPS, Duo, checked, sends

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv

_START, _FUTURE = "2026-01-01T00:00:00+03:00", "2099-01-01T00:00:00+03:00"


@pytest.fixture()
def duo(mv, tmp_path, monkeypatch, fake_awg):
    monkeypatch.setattr(awg, "blocked_ips", lambda: set(fake_awg.blocked), raising=False)
    d = Duo(mv, tmp_path, monkeypatch)
    d.fake = fake_awg
    monkeypatch.setattr(c9deliver, "time", types.SimpleNamespace(monotonic=d.clock.monotonic))
    d.jitter = []
    monkeypatch.setattr(backoff.random, "uniform", lambda lo, hi: d.jitter.append((lo, hi)) or 1.0)
    yield d
    d.close()


def _names(db) -> set[str]:
    return {c.name for c in db.list_clients()}


def _c9_world(duo, edits: int = 2) -> dict:
    """Гость открылся сам (авария), основа ушла в следование и сняла зависший вебхук без
    рычага — эпоха здесь, отметка С9; гость отрезан и успел завести профили — его записи на
    диске резерва, основе не дошли; затем гостя не стало (зона снята)."""
    duo.journal_on()
    duo.edit("Иван")
    duo.tick()
    duo.open_guest(button=False)
    rm.go_away(duo.a, kind="dead")
    duo.a.reserve_epoch_sync()
    assert duo.a.reserve_following() and duo.a._pkg_journal(), "условие: следование журналом"
    duo.a.reserve_stuck_home()
    c9 = duo.a.rsv_get("rsv_c9")
    assert duo.a.reserve_out() == "home" and c9.get("e"), f"условие: дома с отметкой С9: {c9}"
    with duo.as_guest():
        for n in range(edits):
            duo.g.db.create_client(f"Ольга{n}", 1, _START, _FUTURE, f"INV-DUMMY-O{n}")
        duo.g.guest_jrn_tick()                            # на диск резерва; основе не дошло
    _guest_gone(duo)
    return c9


def _guest_gone(duo) -> None:
    if duo.gdb is not None:
        duo.gdb.close()
    duo.gdb, duo.g = None, None
    duo.b.rsv_update("rsv_in", state="idle", kind="")


def _a_st_to_b(duo, **over) -> dict:
    """Свежий `st` основы у резерва (её взгляд на свою линию), сессия жива."""
    body = duo.a.reserve_st_body()
    mine = dict(body["jrn"]["mine"], **over)
    checked("st", rm.peer_st(jrn={"mine": mine}))
    duo.b.rsv_update("rsv_peer_state", session=True, st_stale=False, jrn_mine=mine)
    return mine


def _deliver(duo) -> tuple[list, list]:
    """Такт резерва → основа → её ответы резерву: (что ушло основе, что она ответила)."""
    acts = duo.b.reserve_c9_deliver_tick()
    at_a = duo.b_to_a(acts)
    duo.a_to_b(at_a)
    return acts, at_a


# ── основа говорит, что ждёт ────────────────────────────────────────────────

def test_the_primary_tells_in_st_which_edits_it_waits_for_only_as_the_holder(duo):
    c9 = _c9_world(duo)
    mine = duo.a.reserve_st_body()["jrn"]["mine"]
    assert mine.get("c9") == {"e": c9["e"], "upto": c9["upto"]}, mine
    checked("st", rm.peer_st(jrn={"mine": mine}))
    packed = duo.a._st_packed(duo.a.reserve_st_body())
    duo.a.rsv_update("rsv_c9", upto=int(c9["upto"]) + 1)
    assert duo.a._st_packed(duo.a.reserve_st_body()) != packed, "перенесённая часть не меняет st — резерв не узнает"
    duo.a.db.jrn_pass(holder="peer", role="follower", why="тест", fork_i=duo.a.db.jrn_meta()["applied"])
    assert "c9" not in duo.a.reserve_st_body()["jrn"]["mine"], \
        "следующий зовёт журнал гостя — тот лёг бы у него событиями держателя"


# ── резерв доставляет ───────────────────────────────────────────────────────

def test_the_reserve_sends_the_guest_journal_itself_and_then_says_there_is_no_more(duo):
    c9 = _c9_world(duo)
    _a_st_to_b(duo)
    acts, at_a = _deliver(duo)
    jev = sends(acts, "jev")
    assert len(jev) == 1 and jev[0][1]["e"] == c9["e"] and jev[0][1]["from"] == int(c9["upto"]) + 1, acts
    assert {"Ольга0", "Ольга1"} <= _names(duo.a.db), "журнал гостя с диска не перенесён"
    assert sends(at_a, "jack"), at_a
    upto, moved = int(duo.a.rsv_get("rsv_c9")["upto"]), int(duo.a.rsv_get("rsv_c9")["moved"])
    assert moved > 0
    acts, at_a = _deliver(duo)                            # подтверждение — продвижение: следующий шаг сразу
    assert sends(acts) == [("jc9none", {"line": duo.line, "e": c9["e"], "last": upto})], acts
    assert duo.a.rsv_get("rsv_c9") == {}, "«журнала нет» после переданного — отметка осталась"
    notes = [n.text for a in at_a if a[0] == "notify" for n in a[1]]
    assert notes == [rt.tail_moved("c9", "Beta", moved)], f"итог перенесённого не сказан: {at_a}"
    _a_st_to_b(duo)
    assert duo.b.reserve_c9_deliver_tick() == [], "основа больше не ждёт — резерв всё шлёт"


def test_without_an_answer_the_reserve_retries_with_growing_jittered_pauses_and_never_gives_up(duo):
    _c9_world(duo)
    _a_st_to_b(duo)
    at = []
    for _ in range(2 * C9_WAIT_S):
        if sends(duo.b.reserve_c9_deliver_tick(), "jev"):
            at.append(duo.clock.monotonic())
        duo.clock.advance(1)
    gaps = [round(b - a) for a, b in zip(at, at[1:])]
    fast = [g for g, t in zip(gaps, at[1:]) if t - at[0] <= C9_WAIT_S]
    assert fast[:5] == [C9_RETRY_FIRST_S * 2 ** n for n in range(5)], f"паузы не нарастают вдвое: {gaps}"
    assert sum(fast) <= C9_WAIT_S, gaps
    assert gaps[-1] == C9_RETRY_SLOW_S, f"после срока — не реже, а всё ещё быстро или бросил: {gaps}"
    assert duo.jitter and all(lo < 1 < hi for lo, hi in duo.jitter), "повторы без разброса — маячок"
    duo.clock.advance(10 * C9_RETRY_SLOW_S)
    assert sends(duo.b.reserve_c9_deliver_tick(), "jev"), "резерв бросил доставку, основа так и ждёт"


def test_a_primary_jack_moves_the_delivery_on_at_once(duo):
    _c9_world(duo)
    _a_st_to_b(duo)
    acts = duo.b.reserve_c9_deliver_tick()
    assert sends(acts, "jev")
    assert duo.b.reserve_c9_deliver_tick() == [], "повтор без паузы"
    duo.a_to_b(duo.b_to_a(acts))                          # jack основы транзитом — резерву
    assert sends(duo.b.reserve_c9_deliver_tick(), "jc9none"), "после подтверждения ждёт паузу вместо шага"


def test_a_primary_jneed_is_served_from_the_guest_journal(duo):
    c9 = _c9_world(duo)
    _a_st_to_b(duo)
    acts = duo.b.reserve_jrn_in("jneed", checked("jneed", {"line": duo.line, "from": int(c9["upto"]) + 1}))
    jev = sends(acts, "jev")
    assert jev and jev[0][1]["e"] == c9["e"] and jev[0][1]["from"] == int(c9["upto"]) + 1, \
        f"jneed основы при ожидании С9 обслужен не журналом гостя: {acts}"


def test_after_a_restart_the_reserve_says_no_journal_on_the_first_fresh_st(duo):
    """Журнала гостя на диске нет вовсе (зона снята с ним): после перезапуска резерв молчит до
    свежего st основы, потом сразу — «журнала нет»; основа снимает отметку фактом."""
    c9 = _c9_world(duo, edits=0)
    import shutil
    shutil.rmtree(rsvzone.VAR_DIR / "jrn", ignore_errors=True)
    _a_st_to_b(duo)
    duo.b.__dict__.pop("_c9d_s", None)
    duo.b.reserve_session_reset()                         # старт процесса: st основы — из прошлого
    assert duo.b.reserve_c9_deliver_tick() == [], "по st прошлого процесса"
    _a_st_to_b(duo)
    acts, _ = _deliver(duo)
    assert sends(acts) == [("jc9none", {"line": duo.line, "e": c9["e"], "last": 0})], acts
    assert duo.a.rsv_get("rsv_c9") == {}, "«журнала нет» не сняло отметку"


def test_after_a_restart_the_reserve_sends_the_journal_it_still_has(duo):
    c9 = _c9_world(duo)
    duo.b.reserve_session_reset()
    duo.b.__dict__.pop("_c9d_s", None)
    _a_st_to_b(duo)
    acts, _ = _deliver(duo)
    assert sends(acts, "jev") and sends(acts, "jev")[0][1]["from"] == int(c9["upto"]) + 1, acts
    assert "Ольга0" in _names(duo.a.db)


def test_a_live_guest_sends_its_own_and_the_reserve_keeps_quiet(duo):
    _c9_world(duo)
    duo.b.rsv_update("rsv_in", state="serving", kind="dead")
    _a_st_to_b(duo)
    assert duo.b.reserve_c9_deliver_tick() == [], "резерв шлёт журнал за живого гостя"


def test_an_unreadable_journal_directory_is_not_no_journal(duo, monkeypatch):
    _c9_world(duo)
    _a_st_to_b(duo)
    import os as _os
    real = _os.listdir

    def denied(path):
        if str(path) == str(rsvzone.VAR_DIR / "jrn"):
            raise PermissionError(13, "нет доступа")
        return real(path)
    monkeypatch.setattr(c9deliver.os, "listdir", denied)
    assert duo.b.reserve_c9_deliver_tick() == [], "нечитаемый каталог сошёл за «журнала нет»"
    assert duo.a.rsv_get("rsv_c9").get("e")


# ── основа: слово «журнала нет» ─────────────────────────────────────────────

def _none(duo, e: int, last: int) -> list:
    return duo.a.reserve_jrn_in("jc9none", checked("jc9none", {"line": duo.line, "e": e, "last": last}))


def test_no_journal_beyond_what_was_moved_does_not_drop_the_mark(duo):
    c9 = _c9_world(duo)
    _none(duo, int(c9["e"]), int(c9["upto"]) + 2)
    assert duo.a.rsv_get("rsv_c9").get("e"), "журнал у резерва дальше перенесённого — а отметку сняли"


def test_no_journal_of_another_epoch_does_not_drop_the_mark(duo):
    c9 = _c9_world(duo)
    _none(duo, int(c9["e"]) + 1, 0)
    assert duo.a.rsv_get("rsv_c9").get("e")


def test_no_journal_under_the_lever_is_left_to_the_return(duo):
    c9 = _c9_world(duo)
    duo.a.rsv_update("rsv_out", await_merge=True)
    _none(duo, int(c9["e"]), int(c9["upto"]))
    assert duo.a.rsv_get("rsv_c9").get("e"), "слово сняло отметку под рычагом — возврат без хвоста С9"


def test_no_journal_at_all_drops_the_mark_quietly(duo):
    c9 = _c9_world(duo, edits=0)
    assert _none(duo, int(c9["e"]), 0) == [], "итог без единой перенесённой правки"
    assert duo.a.rsv_get("rsv_c9") == {}


def test_the_word_is_skipped_on_a_line_that_is_not_mine(duo):
    """Слово о чужой линии (у резерва — линия, которую он хранит) — молча мимо, гостю не уходит."""
    _c9_world(duo)
    duo.b.rsv_update("rsv_in", state="serving", kind="dead")
    assert duo.b.reserve_jrn_in("jc9none", checked("jc9none", {"line": duo.line, "e": 1, "last": 0})) == []
    assert duo.b.reserve_jrn_in("jc9none", checked("jc9none", {"line": "f" * 16, "e": 1, "last": 0})) == []


# ── «не ждать?» — по факту ──────────────────────────────────────────────────

def _ago(seconds: float) -> float:
    return timeutil.now().timestamp() - seconds


def _card(svc) -> str:
    return st_t.card_text(svc.reserve_card_data(), "test_bot")


def test_while_the_journal_is_arriving_there_is_no_link_and_drop_waits(duo):
    _c9_world(duo, edits=1)
    _a_st_to_b(duo)
    _deliver(duo)
    c9 = duo.a.rsv_get("rsv_c9")
    assert c9.get("got_ts") and abs(float(c9["got_ts"]) - timeutil.now().timestamp()) < 5, c9
    duo.a.rsv_update("rsv_c9", at_ts=_ago(C9_WAIT_S + 1))
    v = duo.a.reserve_c9_wait_view()
    assert not v["overdue"], "срок вышел при идущем журнале"
    assert "не ждать?" not in _card(duo.a), "ссылка при идущем журнале — админ сам оборвёт доставку"
    with pytest.raises(ServiceError, match="журнал с «Beta» приходит — подожди"):
        duo.a.reserve_c9_drop()
    assert duo.a.rsv_get("rsv_c9").get("e")


def test_after_the_term_without_progress_the_link_shows_even_with_a_journal_on_the_disk(duo):
    """Журнал на диске резерва сам по себе больше не держит: срок C9_WAIT_S без продвижения —
    ссылка и снятие (резерв, который не довёз, — повод решать админу)."""
    c9 = _c9_world(duo)
    keep = dict(duo.b.reserve_keep_view(), gj_e=c9["e"], gj_last=int(c9["upto"]) + 3)
    rm.st_in(duo.a, caps=JCAPS, jrn={"keep": keep}, **{"in": "idle"})
    duo.a.rsv_update("rsv_c9", at_ts=_ago(C9_WAIT_S + 1), got_ts=_ago(C9_PROGRESS_S + 1))
    v = duo.a.reserve_c9_wait_view()
    assert v["overdue"] and not v["drop_why"], v
    assert "не ждать?" in _card(duo.a), _card(duo.a)
    assert duo.a.reserve_c9_drop(), "снятие отказало по журналу на диске резерва"
    assert duo.a.rsv_get("rsv_c9") == {}


def test_before_the_term_there_is_no_link_whatever_the_reserve_does(duo):
    _c9_world(duo)
    duo.a.rsv_update("rsv_c9", at_ts=_ago(C9_WAIT_S - 30))
    assert not duo.a.reserve_c9_wait_view()["overdue"]
