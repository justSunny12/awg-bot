"""Пульт у основы (domain/services/reserve/pult): вебхук бота основы стоит у мёртвого
резерва — Telegram отдаёт обновления туда, и основа не получает ни одного (рычаг «вернуть
без резерва» нажать нечем). Основа в away, линк пары молчит, getWebhookInfo 5 минут
подряд показывает свежие ошибки доставки и растущую очередь — `deleteWebhook` без
сброса очереди, поллинг здесь, пользователи и запись на месте, журнал в следовании.

- условие: проба в такте away не чаще раза в минуту (разброс вверх до 40 %), только
  при молчащем линке и в «чистом» away; счёт подряд прерывают живой линк, проба без
  ответа или без свежей ошибки доставки, убывшая очередь; порог — PULT_AFTER_S и рост
  очереди с начала счёта;
- взятие: отметка в `rsv_out` (переживает рестарт), уведомление со звуком (из CLI —
  без него), `tg_pult` в `st`, следование не снимается; отметка — только эпизода
  away/returning: дома и в новом эпизоде её нет, сорванный возврат обратно в away её
  держит; 409 поллинга (вебхук снова у гостя) снимает её с итогом админу;
- отдача: «📤 Отдать Telegram резерву» — только при живом линке, `pult1` и пока
  резерв несёт моих, слово `pult_back` с отметкой, «отдаю…» — не дольше PULT_GIVE_WAIT_S;
- сторона резерва: отметка в `st` основы — гостю вебхук не ставить (`tg_hold` в слове
  state, guest_can_take_tg), `pult_back` с той же отметкой снимает запрет и велит гостю
  ставить вебхук снова; админу резерва — одно уведомление на отметку, «вебхук не здесь»
  гостя при известном пульте — без второго;
- протокол: `tg_pult` в `st` необязателен, `pult_back{at}` разбирается.

Окружение — `mv` из _reserve_move (пара, часы автоматов — модель).

Цена ошибки: пульт при живом боте на резерве — два бота на один токен; пульт без
счёта «подряд» — один сбой доставки отнимает Telegram у работающего резерва; пульт без
отметки на диске — рестарт основы теряет его, а пользователи слышат обычный бот, который
ничего не пишет; отметка, пережившая возврат, — следующий переезд стартует в режиме
«бот на техработах»; гость, ставящий вебхук при пульте у основы, — Telegram уходит от
админа, который как раз решает, что делать; отдача без живого линка — слово в никуда,
кнопка «сработала», а Telegram так и остаётся здесь.
"""
from __future__ import annotations

import types

import pytest

from awgbot.core import config
from awgbot.domain.services.reserve import compat
from awgbot.domain.services.reserve.pult import primary as pp
from awgbot.domain.services.types import ServiceError
from awgbot.util import pairproto, timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import notices

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv

HOOK = "https://203.0.113.9:8443/DUMMY"


def _ago(minutes: float) -> str:
    return timeutil.to_iso(timeutil.shift(timeutil.now(), -minutes * 60))


def _bad(pending: int, err_ago: float = 30) -> dict:
    """getWebhookInfo: вебхук гостя стоит, доставка падает, очередь `pending`."""
    return {"url": HOOK, "pending_update_count": pending, "last_error_date": int(timeutil.now().timestamp() - err_ago)}


def _silent(svc) -> None:
    svc.rsv_update("rsv_peer_state", session=False, down_since=_ago(11))
    svc._lv.state = "down"


def _alive(svc, **st) -> None:
    rm.st_in(svc, **st)
    svc._lv.state = "ok"


@pytest.fixture()
def away(mv):
    """Мои пользователи у резерва, он не на связи, основа нездорова (возврат не вмешивается)."""
    rm.go_away(mv.svc, kind="dead")
    mv.svc.db.set_state("last_server_ok", "0")
    _silent(mv.svc)
    return mv


def _age_streak(svc, minutes: float) -> None:
    bad = dict(svc.rsv_get("rsv_out")["pult_bad"])
    bad["since"] = _ago(minutes)
    svc.rsv_update("rsv_out", pult_bad=bad)


def _take(svc, by: str = "auto") -> list:
    return svc.reserve_pult_taken(by)


# ── проба в такте away ──────────────────────────────────────────────────────

def _probes(acts) -> int:
    return len(rm.actions_of(acts, "pult_probe"))


def test_the_away_tick_asks_telegram_no_more_than_once_a_minute_with_jitter_up(away, monkeypatch):
    factors: list[tuple] = []

    def uniform(a, b):
        factors.append((a, b))
        return 1.4
    monkeypatch.setattr(pp, "random", types.SimpleNamespace(uniform=uniform))
    svc, clock = away.svc, away.clock
    assert _probes(svc.reserve_out_tick()) == 1, "первая проба не спрошена"
    assert _probes(svc.reserve_out_tick()) == 0, "две пробы подряд"
    clock.advance(pp.PULT_PROBE_S + 1)
    assert _probes(svc.reserve_out_tick()) == 0, "разброс вверх не учтён — проба строго раз в минуту"
    clock.advance(pp.PULT_PROBE_S * 0.4)
    assert _probes(svc.reserve_out_tick()) == 1
    assert factors and all(f == (1.0, 1.4) for f in factors), "не чаще раза в минуту: разброс только вверх"


@pytest.mark.parametrize("prep", [
    lambda s: s.reserve_set_out("home"),
    lambda s: s.rsv_update("rsv_out", await_merge=True),            # рычаг уже нажат — забирает сам
    lambda s: _take(s),                                             # пульт уже здесь
], ids=["home", "await-merge", "pult"])
def test_no_probe_outside_plain_away(away, prep):
    prep(away.svc)
    assert _probes(away.svc.reserve_out_tick()) == 0


def test_no_probe_and_no_count_while_the_link_is_alive(away):
    svc = away.svc
    svc.reserve_pult_probe_in(_bad(3))
    assert svc.rsv_get("rsv_out")["pult_bad"], "счёт не начался"
    _alive(svc, **{"in": "serving"})
    assert _probes(svc.reserve_out_tick()) == 0, "проба при живом линке"
    assert not svc.rsv_get("rsv_out")["pult_bad"], "живой линк не прервал счёт"


# ── условие ─────────────────────────────────────────────────────────────────

def test_five_minutes_of_failing_delivery_and_a_growing_queue_take_the_pult(away):
    svc = away.svc
    assert svc.reserve_pult_probe_in(_bad(3)) == []
    assert svc.reserve_pult_probe_in(_bad(4)) == [], "пульт раньше PULT_AFTER_S"
    _age_streak(svc, pp.PULT_AFTER_S / 60 - 0.5)
    assert svc.reserve_pult_probe_in(_bad(5)) == [], "пульт раньше PULT_AFTER_S"
    _age_streak(svc, pp.PULT_AFTER_S / 60 + 0.5)
    assert svc.reserve_pult_probe_in(_bad(6)) == [("pult_take",)]
    assert not svc.reserve_pult_on(), "отметка до deleteWebhook — пульт без Telegram"


def test_a_queue_that_does_not_grow_is_not_enough(away):
    svc = away.svc
    svc.reserve_pult_probe_in(_bad(3))
    _age_streak(svc, 6)
    assert svc.reserve_pult_probe_in(_bad(3)) == [], "очередь стоит — никто ничего не ждёт от бота"
    assert svc.rsv_get("rsv_out")["pult_bad"], "счёт не должен рваться: очередь вырастет — пора"
    assert svc.reserve_pult_probe_in(_bad(4)) == [("pult_take",)]


def test_a_shrinking_queue_restarts_the_count(away):
    svc = away.svc
    svc.reserve_pult_probe_in(_bad(5))
    _age_streak(svc, 6)
    assert svc.reserve_pult_probe_in(_bad(4)) == [], "очередь убыла — Telegram доставляет"
    bad = svc.rsv_get("rsv_out")["pult_bad"]
    assert bad["p0"] == 4 and timeutil.elapsed(timeutil.parse_iso(bad["since"])) < 60, bad


@pytest.mark.parametrize("info", [
    None,                                                            # Telegram не ответил
    {"url": HOOK, "pending_update_count": 9, "last_error_date": 0},  # очередь без ошибки доставки
    "old-error",
    {"url": HOOK, "pending_update_count": 0, "last_error_date": "now"},
    {"url": "", "pending_update_count": 9, "last_error_date": "now"},   # вебхука нет
], ids=["no-answer", "no-error", "old-error", "empty-queue", "no-hook"])
def test_a_probe_without_the_failure_restarts_the_count(away, info):
    svc = away.svc
    if info == "old-error":
        info = _bad(9, err_ago=outbound_err_s() + 5)
    elif info and info.get("last_error_date") == "now":
        info = {**info, "last_error_date": int(timeutil.now().timestamp() - 30)}
    svc.reserve_pult_probe_in(_bad(3))
    _age_streak(svc, 6)
    assert svc.reserve_pult_probe_in(info) == []
    assert not svc.rsv_get("rsv_out")["pult_bad"], "счёт подряд пережил пробу без отказа доставки"


def outbound_err_s() -> int:
    from awgbot.domain.services.reserve import outbound_return
    return outbound_return.FORCE_HOOK_ERR_S


def test_a_link_that_came_back_between_probes_takes_nothing(away):
    svc = away.svc
    svc.reserve_pult_probe_in(_bad(3))
    _age_streak(svc, 6)
    _alive(svc, **{"in": "serving"})
    assert svc.reserve_pult_probe_in(_bad(9)) == []
    assert not svc.rsv_get("rsv_out")["pult_bad"]


def test_the_failing_verdict():
    now = 1_000_000
    assert pp.hook_failing({"url": HOOK, "pending_update_count": 1, "last_error_date": now - 60}, now)
    assert not pp.hook_failing({"url": HOOK, "pending_update_count": "много", "last_error_date": now - 60}, now)
    assert not pp.hook_failing({"url": HOOK, "pending_update_count": 1, "last_error_date": None}, now)


# ── взятие ──────────────────────────────────────────────────────────────────

def test_taking_marks_the_pult_tells_the_admin_loudly_and_keeps_following(away):
    svc = away.svc
    acts = _take(svc)
    assert svc.reserve_pult_on() and svc.reserve_pult_view()["at"]
    notes = notices(acts)
    assert len(notes) == 1 and notes[0].force_sound and notes[0].tg_id == int(config.ADMIN_ID), notes
    assert notes[0].text == (
        "📍 Управление ботом возвращено на этот сервер\n"
        "Гостевой экземпляр на «Beta» не отвечал 5 минут, прямая связь с сервером отсутствует.\n\n"
        "Пользователи пока остаются на «Beta» (соединение работает штатно). При обращении в бот им "
        "отдаётся сообщение «Бот временно недоступен, VPN работает нормально».\n\n"
        "Дальнейшие действия в меню «🛟 Резерв»:\n"
        "• ↩️ Вернуть пользователей сюда — запустить обратный переезд\n"
        "• 📤 Вернуть бота на «Beta» — снова передать управление ботом на резерв"), notes[0].text
    assert svc.reserve_out() == "away", "пульт сдвинул пользователей"
    assert svc._follow_wanted()[0] is True, "пульт сделал основу держателем — правки разойдутся с ботом на резерве"
    assert svc.reserve_st_body()["tg_pult"] == svc.rsv_get("rsv_out")["pult_at"]
    assert _take(svc) == [], "второе взятие — второе уведомление"



def test_on_the_old_path_without_the_journal_the_admins_edits_under_the_pult_are_refused_too(mv, monkeypatch):
    """Пульт у основы на прежнем пути (SQLite без JSON-функций: захвата и `jrn1` нет): админ
    проходит в бот, а его решения база всё равно отвергает — запрет записи не держателю
    ставится на каждое соединение независимо от JSON-функций, а следование в away — по
    состоянию пары, не по журналу. Правки основы мимо гостя при пульте не ложатся."""
    import sqlite3
    from awgbot.util import rsvjrn
    from awgbot.util.rsvjrn import triggers
    monkeypatch.setattr(triggers, "_CAPTURE_FAILED", {})
    monkeypatch.setattr(triggers, "json_ok", lambda conn: False)
    monkeypatch.setattr(rsvjrn, "json_ok", lambda conn: False)
    monkeypatch.setattr(compat, "_JSON_OK", {})
    svc = mv.svc
    svc.db.close()                                            # новое соединение — триггеры прежнего пути
    names = [r[0] for r in svc.db._connection().execute("SELECT name FROM sqlite_temp_master WHERE type = 'trigger'")]
    assert names and not [n for n in names if n.startswith("jrn_cap_")], f"условие: захват встал — {names}"
    assert not compat.journal_capable() and "jrn1" not in compat.caps(), "условие: путь не прежний"
    rm.go_away(svc, kind="dead")
    svc.db.set_state("last_server_ok", "0")
    _silent(svc)
    _take(svc)
    assert svc.reserve_pult_on() and svc.reserve_following(), "условие: пульт здесь, следование"
    with pytest.raises(sqlite3.IntegrityError) as e:
        svc.db.create_client("Иван", 3, "2026-01-01T00:00:00+03:00", "2099-01-01T00:00:00+03:00", "INV-DUMMY-Ivan")
    assert rsvjrn.is_refusal(e.value), e.value
    assert not svc.db._connection().execute("SELECT 1 FROM clients WHERE name = 'Иван'").fetchone(), \
        "решение админа основы легло мимо гостя"

def test_taking_from_the_terminal_is_silent(away):
    acts = _take(away.svc, "cli")
    assert away.svc.reserve_pult_on() and notices(acts) == []


def test_taking_after_the_state_moved_on_marks_nothing(away):
    away.svc.reserve_set_out("home")
    assert _take(away.svc) == [] and not away.svc.rsv_get("rsv_out").get("pult_at")


def test_the_mark_survives_a_restart_of_the_bot(away):
    _take(away.svc)
    again = type(away.svc)(away.svc.db)
    assert again.reserve_pult_on() and again.reserve_pult_st()


def test_home_and_a_new_episode_drop_the_mark_a_failed_return_keeps_it(away):
    svc = away.svc
    _take(svc)
    svc.reserve_set_out("returning", step="back_req")
    assert svc.reserve_pult_on(), "в возврате пульт ещё здесь"
    svc.reserve_set_out("away")
    assert svc.reserve_pult_on(), "сорванный возврат снял пульт — пользователям снова обычный бот"
    svc.reserve_set_out("home")
    assert not svc.reserve_pult_on() and not svc.rsv_get("rsv_out")["pult_at"]
    rm.go_away(svc, kind="dead")
    assert not svc.reserve_pult_on() and "tg_pult" not in svc.reserve_st_body()


def test_a_webhook_back_at_the_reserve_drops_the_mark_and_says_so(away):
    svc = away.svc
    _take(svc)
    acts = svc.reserve_pult_webhook_back()
    assert not svc.reserve_pult_on()
    assert [n.text for n in notices(acts)] == ["📤 Управление ботом передано на «Beta»"]
    assert svc.reserve_pult_webhook_back() == [], "второе 409 — второе уведомление"


# ── отдача ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("prep, word", [
    (lambda s: None, "не на связи"),
    (lambda s: _alive(s, **{"in": "serving", "caps": [c for c in compat.CAPS if c != "pult1"]}), "не умеет"),
    (lambda s: _alive(s, **{"in": "idle"}), "не несёт"),
    (lambda s: (s.reserve_set_out("home")), "не у этого сервера"),
], ids=["silent", "no-cap", "not-hosting", "no-pult"])
def test_giving_telegram_back_is_refused_with_the_reason(away, prep, word):
    svc = away.svc
    _take(svc)
    prep(svc)
    assert word in svc.reserve_pult_give_check()
    with pytest.raises(ServiceError):
        svc.reserve_pult_give()


def test_giving_sends_the_mark_and_shows_giving_for_a_while(away):
    svc, clock = away.svc, away.clock
    _take(svc)
    _alive(svc, **{"in": "serving"})
    assert svc.reserve_pult_give_check() == ""
    at = svc.rsv_get("rsv_out")["pult_at"]
    acts = svc.reserve_pult_give()
    assert ("send", "pult_back", {"at": at}) in acts
    assert svc.reserve_pult_on(), "пульт снят нажатием, а не фактом 409"
    assert svc.reserve_pult_view()["giving"] and "уже отдаётся" in svc.reserve_pult_give_check()
    clock.advance(pp.PULT_GIVE_WAIT_S + 1)
    assert not svc.reserve_pult_view()["giving"] and svc.reserve_pult_give_check() == "", "кнопка навсегда «отдаётся»"


# ── сторона резерва ─────────────────────────────────────────────────────────

AT = "2026-11-01T10:00:00+05:00"


@pytest.fixture()
def hosting(mv):
    """У меня пользователи второго сервера и его бот; его линк жив."""
    mv.svc.reserve_set_in("serving", kind="dead", tg_taken=True, taken_at=timeutil.now_iso())
    return mv


def test_the_pult_mark_of_the_primary_holds_the_guest_webhook_until_given_back(hosting):
    svc = hosting.svc
    assert svc.reserve_pult_hold_why() == ""
    notes = rm.st_in(svc, out="away", tg_pult=AT)
    assert svc.reserve_pult_hold_why(), "отметка в st основы не запретила гостю вебхук"
    assert svc.reserve_guest_state_view()["tg_hold"], "гость о запрете не узнает"
    assert [n.text for n in notes] == ["⚠️ Управление ботом «Beta» перехвачено основным сервером.\nВходящие сообщения уходят на «Beta», но его пользователи продолжают обслуживаться здесь"]
    assert notes[0].force_sound
    assert rm.st_in(svc, out="away", tg_pult=AT) == [], "уведомление на каждый st"
    assert svc.reserve_card_data()["peer_pult"]
    acts = svc.reserve_pult_back_in(AT)
    assert acts == [("guest", "tg_retake", {})]
    assert svc.reserve_pult_hold_why() == "" and not svc.reserve_guest_state_view()["tg_hold"]
    assert rm.st_in(svc, out="away", tg_pult=AT) == [], "отданный пульт — снова уведомление"


def test_a_new_pult_mark_holds_again_and_is_told_again(hosting):
    svc = hosting.svc
    rm.st_in(svc, out="away", tg_pult=AT)
    svc.reserve_pult_back_in(AT)
    notes = rm.st_in(svc, out="away", tg_pult="2026-11-01T11:00:00+05:00")
    assert svc.reserve_pult_hold_why() and len(notes) == 1


def test_the_st_without_the_mark_lifts_the_hold(hosting):
    svc = hosting.svc
    rm.st_in(svc, out="away", tg_pult=AT)
    rm.st_in(svc, out="away")
    assert svc.reserve_pult_hold_why() == ""


def test_giving_back_without_hosting_does_nothing(mv):
    assert mv.svc.reserve_pult_back_in(AT) == []


def test_a_foreign_webhook_of_the_guest_is_not_told_twice_when_the_pult_is_known(hosting):
    svc = hosting.svc
    rm.st_in(svc, out="away", tg_pult=AT)
    assert svc.reserve_guest_webhook_in({"foreign": True, "stalled": False}) == []
    assert svc.rsv_get("rsv_in")["webhook_foreign"] is True
    svc.reserve_pult_back_in(AT)
    assert svc.rsv_get("rsv_in")["webhook_foreign"] is False, "следующее «не здесь» гостя не будет сказано"


def test_no_notice_without_my_hosting(mv):
    assert rm.st_in(mv.svc, out="away", tg_pult=AT) == []


# ── гость ───────────────────────────────────────────────────────────────────

def test_the_guest_does_not_take_telegram_while_the_reserve_bot_holds_it(services):
    assert services.guest_can_take_tg()
    services.guest_state_in({"name": "Beta", "tg_hold": "Telegram бота основы у неё — жду, пока отдаст"})
    assert not services.guest_can_take_tg(), "вебхук поверх Telegram у основы — два бота на один токен"
    services.guest_tg_hold_clear()
    assert services.guest_can_take_tg()
    services.guest_state_in({"name": "Beta", "tg_hold": ""})
    assert services.guest_can_take_tg()


# ── протокол ────────────────────────────────────────────────────────────────

def test_the_protocol_carries_the_mark_and_the_word():
    st = rm.peer_st()
    assert "tg_pult" not in pairproto.check(pairproto.PAIR_PROTO, "st", st, pairproto.INVITER), "поле обязательно"
    got = pairproto.check(pairproto.PAIR_PROTO, "st", {**st, "tg_pult": AT}, pairproto.INVITER)
    assert got["tg_pult"] == AT
    assert pairproto.check(pairproto.PAIR_PROTO, "pult_back", {"at": AT}, pairproto.ACCEPTOR) == {"at": AT}
    with pytest.raises(pairproto.ProtocolError):
        pairproto.check(pairproto.PAIR_PROTO, "pult_back", {"at": "x" * 33}, pairproto.ACCEPTOR)
    assert "pult1" in compat.CAPS and len(compat.caps()) <= 16, "caps в st — не больше 16"
