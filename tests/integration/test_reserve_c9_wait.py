"""Отметка С9 без рычага, которую резерв не снимет (journal/c9wait): плашка
карточки основы «жду события», по сроку C9_WAIT_S от заведения — «(не ждать?)»
ссылкой на экран, где админ снимает отметку сам (`reserve_c9_drop`): строка в
журнал и итог уведомлением; повтор и снятая фактом отметка — None, не ошибка;
пока резерв на связи и несёт бот — отказ: правки придут сами; журнал гостя на
диске резерва сам по себе не держит — его доставляет резерв, а пока части идут,
держит прогресс (test_reserve_c9_deliver). С рычагом (await_merge) отметку ведёт возврат —
ни плашки, ни снятия. Отметка прежней версии без `at_ts` — срок по `at`, без
обоих — от первого такта дома.

Цена ошибки: без плашки и снятия отметка резерва, который не вернётся, висит
бессрочно (перехват событий старой эпохи, ожидание чужого журнала, Т-Ж4/Т-Ж5 с
устаревшими цифрами); ссылка до срока или при живом госте — админ сам теряет
правки, которые вот-вот придут; снятие под рычагом — возврат без хвоста С9;
срок прежней отметки «сразу истёк» — то же без единого повода.
"""
from __future__ import annotations

import pytest

from awgbot.bot.texts.reserve import c9wait as ct, section as st_t
from awgbot.domain.services import ServiceError
from awgbot.domain.services.reserve.journal.c9wait import C9_WAIT_S
from awgbot.infra import awg
from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_jrn_duo import JCAPS, Duo

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv


@pytest.fixture()
def duo(mv, tmp_path, monkeypatch, fake_awg):
    monkeypatch.setattr(awg, "blocked_ips", lambda: set(fake_awg.blocked), raising=False)
    d = Duo(mv, tmp_path, monkeypatch)
    d.fake = fake_awg
    yield d
    d.close()


def _ago(seconds: float) -> float:
    return timeutil.now().timestamp() - seconds


def _down(svc) -> None:
    svc.rsv_update("rsv_peer_state", session=False, down_since=timeutil.to_iso(timeutil.shift(timeutil.now(), -600)))


def _home_with_c9(duo) -> dict:
    """Следование журналом, зависший вебхук снят без рычага: эпоха здесь, отметка С9; резерв не на связи."""
    duo.journal_on()
    duo.edit("Иван")
    duo.tick()
    duo.open_guest(button=False)
    rm.go_away(duo.a, kind="dead")
    duo.a.reserve_epoch_sync()
    assert duo.a.reserve_following() and duo.a._pkg_journal(), "условие: следование журналом"
    duo.a.reserve_stuck_home()
    _down(duo.a)
    c9 = duo.a.rsv_get("rsv_c9")
    assert duo.a.reserve_out() == "home" and c9.get("e"), f"условие: дома с отметкой С9: {c9}"
    return c9


def _card(svc) -> str:
    return st_t.card_text(svc.reserve_card_data(), "test_bot")


# ── плашка и срок ───────────────────────────────────────────────────────────

def test_the_mark_carries_its_start_and_the_card_waits_without_a_link_before_the_term(duo):
    c9 = _home_with_c9(duo)
    assert abs(float(c9["at_ts"]) - timeutil.now().timestamp()) < 5, f"нет момента заведения: {c9}"
    v = duo.a.reserve_c9_wait_view()
    assert v and not v["overdue"] and not v["online"], v
    card = _card(duo.a)
    assert "└ ⏳ жду журнал событий с «Beta»" in card, card
    assert "не ждать?" not in card, "ссылка «не ждать» до срока — админ снимет ожидание правок, которые вот-вот придут"


def test_after_the_term_the_card_links_to_the_drop_screen(duo):
    _home_with_c9(duo)
    duo.a.rsv_update("rsv_c9", at_ts=_ago(C9_WAIT_S + 1))
    assert duo.a.reserve_c9_wait_view()["overdue"]
    card = _card(duo.a)
    assert f'└ ⏳ жду журнал событий с «Beta» (<a href="https://t.me/test_bot?start={ct.C9_PAYLOAD}">не ждать?</a>)' in card, card
    # без имени бота ссылки не собрать — хвост не выводится вовсе (простой текст «(не ждать?)» никуда не вёл)
    plain = st_t.card_text(duo.a.reserve_card_data(), "")
    assert "└ ⏳ жду журнал событий с «Beta»\n" in plain + "\n" and "не ждать" not in plain, plain


def test_the_term_counts_from_the_start_not_from_now(duo):
    _home_with_c9(duo)
    duo.a.rsv_update("rsv_c9", at_ts=_ago(C9_WAIT_S - 30))
    duo.a.reserve_out_tick()
    assert not duo.a.reserve_c9_wait_view()["overdue"], "срок вышел раньше C9_WAIT_S"


def test_a_live_guest_hides_the_link_even_after_the_term(duo):
    """Резерв на связи и несёт бот: правки придут сами — ссылки нет, снятие отказывает."""
    _home_with_c9(duo)
    duo.a.rsv_update("rsv_c9", at_ts=_ago(C9_WAIT_S + 1))
    rm.st_in(duo.a, caps=JCAPS, jrn={"keep": duo.b.reserve_keep_view()}, **{"in": "serving"})
    v = duo.a.reserve_c9_wait_view()
    assert v["online"] and v["drop_why"], v
    assert "не ждать?" not in _card(duo.a)
    with pytest.raises(ServiceError, match="управляет ботом"):
        duo.a.reserve_c9_drop()
    assert duo.a.rsv_get("rsv_c9").get("e"), "отказ снял отметку"


def test_an_old_mark_without_at_ts_counts_from_its_at(duo):
    _home_with_c9(duo)
    old = dict(duo.a.rsv_get("rsv_c9"))
    old.pop("at_ts")
    old["at"] = timeutil.to_iso(timeutil.shift(timeutil.now(), -(C9_WAIT_S + 60)))
    duo.a.rsv_put("rsv_c9", old)
    assert duo.a.reserve_c9_wait_view()["overdue"], "отметка прежней версии — срок не по её at"


def test_an_old_mark_without_any_start_counts_from_the_first_home_tick(duo):
    """Ни at_ts, ни at: срок не «уже истёк» — отсчёт с такта, который её увидел."""
    _home_with_c9(duo)
    old = {k: v for k, v in duo.a.rsv_get("rsv_c9").items() if k not in ("at", "at_ts")}
    duo.a.rsv_put("rsv_c9", old)
    assert not duo.a.reserve_c9_wait_view()["overdue"]
    duo.a.reserve_out_tick()
    c9 = duo.a.rsv_get("rsv_c9")
    assert c9.get("e") and abs(float(c9["at_ts"]) - timeutil.now().timestamp()) < 5, c9
    assert not duo.a.reserve_c9_wait_view()["overdue"]


# ── «🗑 Не ждать» ───────────────────────────────────────────────────────────

def test_drop_removes_the_mark_logs_and_tells_the_admin(duo, caplog):
    from awgbot.domain.notices import reserve_tail as rt
    _home_with_c9(duo)
    duo.a.rsv_update("rsv_c9", at_ts=_ago(C9_WAIT_S + 1))
    caplog.set_level("WARNING")
    notes = duo.a.reserve_c9_drop()
    assert duo.a.rsv_get("rsv_c9") == {}, "отметка осталась"
    assert [n.text for n in notes] == [rt.c9_dropped("Beta")], notes
    assert any("отметка С9 снята админом" in r.getMessage() for r in caplog.records), "нет строки в журнале"
    assert duo.a.reserve_c9_wait_view() == {}
    assert "жду журнал событий" not in _card(duo.a)


def test_a_second_drop_or_a_mark_settled_meanwhile_is_none(duo):
    _home_with_c9(duo)
    assert duo.a.reserve_c9_drop()
    assert duo.a.reserve_c9_drop() is None, "повторное нажатие — не None"
    duo.a.reserve_c9_start(5)
    duo.a.rsv_put("rsv_c9", None)                         # снята фактом между экраном и нажатием
    assert duo.a.reserve_c9_drop() is None


def test_the_guest_journal_on_the_reserve_disk_alone_no_longer_refuses_the_drop(duo):
    """Прежде журнал на диске резерва отказывал «придут сами» — бессрочно, если резерв его
    не довозил. Теперь доставляет резерв, а держит только прогресс (часть за C9_PROGRESS_S)."""
    _home_with_c9(duo)
    c9 = duo.a.rsv_get("rsv_c9")
    keep = dict(duo.b.reserve_keep_view(), gj_e=c9["e"], gj_last=int(c9["upto"]) + 3)
    rm.st_in(duo.a, caps=JCAPS, jrn={"keep": keep}, **{"in": "idle"})
    assert duo.a.reserve_c9_wait_view()["drop_why"] == "", "журнал на диске без прогресса держит снятие"
    assert duo.a.reserve_c9_drop(), "снятие отказало по журналу на диске резерва"
    assert duo.a.rsv_get("rsv_c9") == {}


def test_the_lever_mark_has_no_plaque_and_is_not_dropped(duo):
    """С рычагом (await_merge) отметку ведёт возврат — плашки и снятия нет."""
    duo.journal_on()
    duo.edit("Иван")
    duo.tick()
    duo.open_guest(button=False)
    rm.go_away(duo.a, kind="dead")
    duo.a.reserve_epoch_sync()
    rm.automatic(duo.a)
    duo.a.db.set_state("last_server_ok", "0")
    _down(duo.a)
    duo.a.rsv_update("rsv_peer_state", down_since=timeutil.to_iso(timeutil.shift(timeutil.now(), -11 * 60)))
    duo.a.reserve_force_home()
    duo.a.reserve_stuck_home()
    assert duo.a.rsv_get("rsv_out").get("await_merge") and duo.a.rsv_get("rsv_c9").get("e"), "условие: рычаг с С9"
    duo.a.rsv_update("rsv_c9", at_ts=_ago(C9_WAIT_S + 1))
    assert duo.a.reserve_c9_wait_view() == {}
    assert duo.a.reserve_c9_drop() is None
    assert duo.a.rsv_get("rsv_c9").get("e"), "снятие под рычагом — возврат без хвоста С9"


# ── экран ───────────────────────────────────────────────────────────────────

def test_the_screen_tells_what_is_lost_and_what_is_known():
    v = {"since": "2026-09-15T11:50:00+03:00", "overdue": True, "online": False,
         "down_since": "2026-09-15T11:40:00+03:00", "moved": 2, "drop_why": ""}
    t = ct.screen_text(v, "Beta")
    assert t.startswith("⚠️ <b>Не ждать изменений от «Beta»?</b>\n\n"), t
    assert "будут безвозвратно утеряны" in t and t.endswith("\nУже синхронизировано изменений: 2"), t
    assert "«Beta» недоступен с " in t and "Запущен ли на нём ещё гостевой экземпляр — неизвестно." in t, t
    assert not t.endswith("."), "текст экрана с точкой в конце"
    assert "Уже синхронизировано" not in ct.screen_text(dict(v, moved=0), "Beta"), "ноль выведен"
    assert "Ожидание уже отключено" in ct.screen_text({}, "Beta")
