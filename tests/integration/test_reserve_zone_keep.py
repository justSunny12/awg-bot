"""Срыв приёма после смены записи процессом открытия — зона остаётся (inbound_keep,
inbound `_take_fail`, opening `reserve_open_by_points`, outbound_keep).

Сторона резерва, аварийный путь (по точкам кворума или по коду): процесс открытия
сменил запись DNS на этот сервер (шаг `record_set`, `record_fact`, `done` без
`record=skipped`, итог `fail` с `record=set`), а дальше — срыв любой причины: факт
записи не подтвердился за 30 минут, процесс убит или вышел без итога, срок 35 минут,
зона не приняла serve. Одно правило: зона не снимается, `down` основе нет.
- клоны стоят — serving без `tg_taken`, админу резерва громко, строка в карточке,
  `st.in_keep`; факт записи такт проверяет сам (раз в минуту с разбросом) и по факту
  кладёт `tg_take`, serve повторяет;
- клонов нет — перезапуск процесса открытия тем же входом не чаще раза в минуту с
  разбросом; основа жива и сказала это `st` после срыва — прежний срыв с `down{rec}`;
  входа в памяти нет (бот перезапущен) — прежний срыв без `rec`;
- путь с K основы и срыв без смены записи — как прежде; снимают зону `back` основы и
  снятие мимо бота.

Сторона основы: `st.in_keep` — её админу уведомление один раз на эпизод; `down{rec}` —
запись на себя только по живому `st` резерва после слова и только если он моих не несёт.

Мир — фикстуры host (test_reserve_hosting) и mv (_reserve_move): зона, процессы, часы и
DNS подставные.

Цена ошибки: зона, снятая после смены записи, — пользователи идут на адрес резерва, где
клона уже нет, а основа мертва: без VPN до ручного вмешательства, хотя клон работал;
перезапуск без паузы или строго периодический — маячок и нагрузка на мёртвый провайдер;
запись, возвращённая основе без её живого слова, — пользователи уходят к мёртвой; без
`down{rec}` у живой основы — запись навсегда на резерве без клона.
"""
from __future__ import annotations

import time

import pytest

from awgbot.bot.texts.reserve.section import card_text
from awgbot.domain.services.reserve import inbound, inbound_keep, inbound_tgwait
from awgbot.infra import rsvzone
from awgbot.util import backoff, pairproto

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import OWN_IP, notices, sent
from tests.integration.test_reserve_hosting import _taking, host, payload_of

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["host"]

NAME, IP = "vpn.example.net", "203.0.113.20"
POINTS = {"qpoints": [[1, "ab" * 16], [2, "cd" * 16]], "qbox": "00" * 56}


@pytest.fixture(autouse=True)
def _steady(monkeypatch):
    monkeypatch.setattr(backoff.random, "uniform", lambda a, b: 1.0)   # разброс — отдельным тестом


def _emergency(host, *, moved: bool = True) -> None:
    """Аварийное открытие идёт; процесс сменил запись (шаг record_set прочитан тактом)."""
    _taking(host, "dead")
    host.svc.rsv_update("rsv_in", by="share")
    if moved:
        host.zone.status = {"step": "record_set", "name": NAME, "ip": IP}
        host.svc.reserve_in_tick()


def _by_points(host, monkeypatch) -> None:
    """Открытие по точкам кворума (вход процесса — в памяти бота) до смены записи."""
    svc = host.svc
    monkeypatch.setattr(svc, "reserve_auto_block", lambda: "")
    monkeypatch.setattr(svc, "reserve_zone_shell_ensure", lambda: None)
    svc.reserve_open_by_points(dict(POINTS), "dead", "q")
    assert svc.reserve_in() == "taking" and len(host.spawn.popen) == 1, "подготовка: открытие не пошло"
    svc.rsv_update("rsv_in", deadline=host.clock.t + inbound.OPEN_WAIT_S)   # срок — по часам теста (opening их не знает)
    host.zone.status = {"step": "record_set", "name": NAME, "ip": IP}
    svc.reserve_in_tick()


def _no_teardown(host) -> None:
    assert not {"teardown", "wipe"} & set(host.zone.names()), f"зона снята после смены записи: {host.zone.calls}"


# ── клоны стоят: зона остаётся, не доделанное — доделывается ─────────────────

def test_a_record_fact_timeout_keeps_the_zone_loudly(host):
    svc = host.svc
    _emergency(host)
    host.zone.status = {"step": "fail", "reason": "факт записи не подтвердился за 30 минут", "record": "set",
                        "name": NAME, "ip": IP}
    acts = svc.reserve_in_tick()
    inn = svc.rsv_get("rsv_in")
    assert svc.reserve_in() == "serving" and not inn.get("tg_taken") and inn.get("keep_k") == "record", inn
    _no_teardown(host)
    assert "down" not in sent(acts), "основе — «не принял», а пользователи уже здесь"
    note = notices(acts)[0]
    assert note.force_sound and "VPN работает" in note.text and "не подтвердилась" in note.text, note
    assert "факт записи не подтвердился за 30 минут" in note.text and "проверяю запись раз в минуту" in note.text
    assert not host.zone.take.exists(), "отметка tg_take раньше факта записи — два бота на один токен"
    card = card_text(svc.reserve_card_data())
    assert "└ ⚠️ DNS-запись не подтвердилась: факт записи не подтвердился за 30 минут — проверяю раз в минуту" in card, card
    keep = svc.reserve_st_body()["in_keep"]
    assert keep["k"] == "record" and "30 минут" in keep["why"], keep
    assert svc.reserve_in_tick() == [], "уведомление о том же сбое повторилось"


def test_the_fact_is_then_checked_by_the_bot_and_the_steps_are_finished(host, monkeypatch):
    svc = host.svc
    _emergency(host)
    host.zone.status = {"step": "fail", "reason": "факт записи не подтвердился за 30 минут", "record": "set"}
    svc.reserve_in_tick()
    host.dns.auth = [OWN_IP]                                  # авторитетный сервер ещё отдаёт прежний адрес
    host.clock.advance(inbound_keep.KEEP_RETRY_S - 5)
    reads = host.dns.auth_reads
    svc.reserve_in_tick()
    assert host.dns.auth_reads == reads, "проверка раньше срока"
    host.clock.advance(10)
    svc.reserve_in_tick()
    assert host.dns.auth_reads == reads + 1 and not host.zone.take.exists()
    host.dns.auth = [IP]
    host.zone.calls.clear()
    host.clock.advance(inbound_keep.KEEP_RETRY_S + 1)
    acts = svc.reserve_in_tick()
    inn = svc.rsv_get("rsv_in")
    assert host.zone.take.read_text(encoding="utf-8") == "1", "факт есть, а отметки «ставь вебхук» нет"
    assert host.zone.names() == ["serve"] and ("fw",) in acts, f"режим serve зоны не включён после факта: {host.zone.calls}"
    assert inn.get("rec_fact") and not inn.get("serve_due") and not inn.get("keep_k"), inn
    acts = svc.reserve_guest_tg_taken()
    assert "taken" in sent(acts) and svc.reserve_in() == "serving" and svc.rsv_get("rsv_in").get("tg_taken")
    assert "in_keep" not in svc.reserve_st_body() and "не подтвердилась" not in card_text(svc.reserve_card_data())


def test_an_unreadable_fact_is_just_retried(host):
    svc = host.svc
    _emergency(host)
    host.zone.status = {"step": "fail", "reason": "факт записи не подтвердился за 30 минут", "record": "set"}
    svc.reserve_in_tick()
    host.dns.auth_error = True
    host.clock.advance(inbound_keep.KEEP_RETRY_S + 1)
    assert svc.reserve_in_tick() == [] and svc.reserve_in() == "serving" and not host.zone.take.exists()
    _no_teardown(host)


def test_the_fail_field_alone_is_enough(host):
    """Шаг record_set такт мог не застать — итог `fail` с record=set говорит о смене сам."""
    svc = host.svc
    _emergency(host, moved=False)
    host.zone.status = {"step": "fail", "reason": "факт записи не подтвердился за 30 минут", "record": "set",
                        "name": NAME, "ip": IP}
    svc.reserve_in_tick()
    inn = svc.rsv_get("rsv_in")
    assert svc.reserve_in() == "serving" and inn.get("rec_name") == NAME and inn.get("rec_ip") == IP, inn
    _no_teardown(host)


@pytest.mark.parametrize("first", ["fail", "exit"])
def test_clone_up_missed_by_the_tick_is_done_on_keep(host, first):
    """Такт не застал ни одного шага после подъёма клона — первым прочитан итог `fail record=set`
    или выход процесса на record_set: `up` основе и сброс conntrack по портам клонов выдаёт
    срыв с сохранением зоны (в serving их не выдаст никто), отметка уступившего хвоста снята."""
    svc = host.svc
    _emergency(host, moved=False)
    svc.rsv_update("rsv_in", relay_flush_due=True)            # хвост основы уступил этому открытию
    if first == "fail":
        host.zone.status = {"step": "fail", "reason": "факт записи не подтвердился за 30 минут", "record": "set",
                            "name": NAME, "ip": IP}
    else:
        host.zone.status = {"step": "record_set", "name": NAME, "ip": IP}
        host.spawn.rc = 137                                   # процесс убит сразу после смены записи
    acts = svc.reserve_in_tick()
    inn = svc.rsv_get("rsv_in")
    assert svc.reserve_in() == "serving", inn
    assert ("relay_flush",) in acts, f"записи conntrack с DNAT на основу не сброшены: {acts}"
    assert acts.index(("fw",)) < acts.index(("relay_flush",)), "сброс раньше перерисовки файервола"
    assert sent(acts).count("up") == 1, acts
    assert inn.get("up_sent") and not inn.get("relay_flush_due"), inn
    _no_teardown(host)
    host.clock.advance(inbound_keep.KEEP_RETRY_S + 1)
    again = svc.reserve_in_tick()
    assert ("relay_flush",) not in again and "up" not in sent(again), again


def test_keep_after_the_tick_saw_clone_up_does_not_repeat_its_words(host):
    """Шаг после подъёма клона такт застал (up и сброс уже выданы) — срыв их не повторяет."""
    svc = host.svc
    _emergency(host)
    assert svc.rsv_get("rsv_in").get("up_sent"), "подготовка: такт не застал record_set"
    host.zone.status = {"step": "fail", "reason": "факт записи не подтвердился за 30 минут", "record": "set",
                        "name": NAME, "ip": IP}
    acts = svc.reserve_in_tick()
    assert svc.reserve_in() == "serving"
    assert ("relay_flush",) not in acts and "up" not in sent(acts), acts

@pytest.mark.parametrize("how", ["killed", "deadline"])
def test_a_process_lost_after_the_record_change_keeps_the_zone(host, how):
    svc = host.svc
    _emergency(host)
    if how == "killed":
        host.spawn.rc = 137                                   # OOM посреди ожидания факта
    else:
        host.clock.advance(inbound.OPEN_WAIT_S + 1)
    acts = svc.reserve_in_tick()
    inn = svc.rsv_get("rsv_in")
    assert svc.reserve_in() == "serving" and inn.get("keep_k") == "record", inn
    reason = "код 137" if how == "killed" else "35 минут"
    assert reason in inn.get("keep_why", "") and reason in notices(acts)[0].text, inn
    assert "down" not in sent(acts)
    _no_teardown(host)


def test_a_serve_failure_after_done_keeps_the_zone_and_retries_serve(host):
    svc = host.svc
    _emergency(host)
    host.zone.status = {"step": "done"}
    host.zone.fail_on["serve"] = "порт вебхука занят"
    acts = svc.reserve_in_tick()
    inn = svc.rsv_get("rsv_in")
    assert svc.reserve_in() == "serving" and inn.get("serve_due") and inn.get("keep_k") == "serve", inn
    assert "порт вебхука занят" in notices(acts)[0].text and "Telegram не пропущен к гостевому серверу" in notices(acts)[0].text
    assert "└ ⚠️ Telegram не пропущен к гостевому серверу: зона не приняла режим serve: порт вебхука занят" in \
        card_text(svc.reserve_card_data())
    host.clock.advance(inbound_keep.KEEP_RETRY_S + 1)
    assert svc.reserve_in_tick() == [] and svc.rsv_get("rsv_in").get("serve_due"), "повтор отказа — молча, без снятия"
    del host.zone.fail_on["serve"]
    host.clock.advance(inbound_keep.KEEP_RETRY_S + 1)
    assert svc.reserve_in_tick() == [("fw",)]
    inn = svc.rsv_get("rsv_in")
    assert not inn.get("serve_due") and inn.get("served") and not inn.get("keep_k"), inn
    _no_teardown(host)


# ── tg_taken раньше, чем такт включил serve ─────────────────────────────────
# Режим serve зоны (вход вебхука) включает такт, увидев done. Слово гостя «вебхук стоит»
# могло прийти раньше (такт прочёл record_fact, а процесс положил tg_take и done следом;
# такт сорвался до serve): переход в serving с отметкой served без serve — вход вебхука
# закрыт навсегда, гость молчит при живом VPN.

def _tg_take_before_done(host) -> None:
    host.zone.status = {"step": "record_fact", "name": NAME, "ip": IP}
    host.svc.reserve_in_tick()                                # такт застал факт записи, done — ещё нет
    host.zone.take.parent.mkdir(parents=True, exist_ok=True)
    host.zone.take.write_text("1", encoding="utf-8")          # процесс положил отметку «ставь вебхук»


def test_tg_taken_before_the_tick_saw_done_turns_serve_on_first(host):
    svc = host.svc
    _emergency(host)
    _tg_take_before_done(host)
    assert "serve" not in host.zone.names(), "подготовка: serve до done"
    acts = svc.reserve_guest_tg_taken()
    inn = svc.rsv_get("rsv_in")
    assert host.zone.names().count("serve") == 1, f"вебхук гостя стоит, а вход вебхука в зону не включён: {host.zone.calls}"
    assert svc.reserve_in() == "serving" and inn.get("tg_taken") and inn.get("served"), inn
    assert "taken" in sent(acts) and ("fw",) in acts
    host.zone.status = {"step": "done"}
    host.clock.advance(inbound_keep.KEEP_RETRY_S + 1)
    svc.reserve_in_tick()
    assert host.zone.names().count("serve") == 1, "serve повторился"
    _no_teardown(host)


def test_a_serve_refused_at_tg_taken_keeps_the_zone_and_is_retried(host):
    svc = host.svc
    _emergency(host)
    _tg_take_before_done(host)
    host.zone.fail_on["serve"] = "порт вебхука занят"
    acts = svc.reserve_guest_tg_taken()
    inn = svc.rsv_get("rsv_in")
    assert host.zone.names() == ["serve"], f"serve не пробовали: {host.zone.calls}"
    assert svc.reserve_in() == "serving" and inn.get("tg_taken"), "Telegram гость уже взял — слово не потерять"
    assert inn.get("serve_due") and not inn.get("served") and inn.get("keep_k") == "serve", inn
    assert "down" not in sent(acts) and "taken" in sent(acts)
    assert "Telegram не пропущен к гостевому серверу" in notices(acts)[0].text
    _no_teardown(host)
    del host.zone.fail_on["serve"]
    host.clock.advance(inbound_keep.KEEP_RETRY_S + 1)
    svc.reserve_in_tick()
    inn = svc.rsv_get("rsv_in")
    assert host.zone.names() == ["serve", "serve"] and inn.get("served") and not inn.get("serve_due"), inn
    assert "in_keep" not in svc.reserve_st_body()


def test_a_serve_refused_at_tg_taken_on_the_key_path_ends_the_hosting_as_on_the_tick(host):
    svc = host.svc
    _taking(host, "button")
    host.zone.status = {"step": "clone_up"}
    svc.reserve_in_tick()
    host.zone.fail_on["serve"] = "порт вебхука занят"
    acts = svc.reserve_guest_tg_taken()
    assert svc.reserve_in() == "idle" and host.zone.names()[-2:] == ["teardown", "wipe"]
    assert "down" in sent(acts) and "taken" not in sent(acts)


def test_tg_taken_after_a_kept_serve_failure_does_not_claim_serve(host):
    """Процесс убит между отметкой tg_take и done: serving с повтором serve, вебхук гостя
    приходит следом — отметка served только по настоящему serve."""
    svc = host.svc
    _emergency(host)
    _tg_take_before_done(host)
    host.spawn.rc = 137
    svc.reserve_in_tick()
    assert svc.reserve_in() == "serving" and svc.rsv_get("rsv_in").get("serve_due"), "подготовка"
    svc.reserve_guest_tg_taken()
    inn = svc.rsv_get("rsv_in")
    assert inn.get("tg_taken") and inn.get("serve_due") and not inn.get("served"), inn
    assert "serve" not in host.zone.names(), "serve без такта — повтор ведёт inbound_keep"


def test_a_bot_failure_after_done_is_the_same_rule_and_loud(host, monkeypatch):
    monkeypatch.setattr(inbound_tgwait, "subprocess", host.spawn)
    monkeypatch.setattr(rsvzone, "guest_unit_active", lambda: False)
    svc = host.svc
    _emergency(host)
    host.zone.status = {"step": "done"}
    svc.reserve_in_tick()
    svc.reserve_in_tick()
    host.clock.advance(inbound.TAKEN_WAIT_S + 1)
    acts = svc.reserve_in_tick()
    assert svc.reserve_in() == "serving" and svc.rsv_get("rsv_in").get("keep_k") == "tg"
    assert notices(acts)[0].force_sound and "не отвечает в Telegram" in notices(acts)[0].text
    assert svc.reserve_st_body()["in_keep"]["k"] == "tg"
    _no_teardown(host)


def test_back_of_the_primary_takes_the_kept_zone_down(host):
    svc = host.svc
    _emergency(host)
    host.zone.status = {"step": "fail", "reason": "факт записи не подтвердился за 30 минут", "record": "set"}
    svc.reserve_in_tick()
    acts = svc.reserve_back_in()
    assert svc.reserve_in() == "relay" and "wipe" in host.zone.names() and sent(acts)[-1] == "wiped"


def test_the_zone_removed_past_the_bot_ends_the_kept_hosting(host):
    svc = host.svc
    _emergency(host)
    host.zone.status = {"step": "fail", "reason": "факт записи не подтвердился за 30 минут", "record": "set"}
    svc.reserve_in_tick()
    host.zone.phase = "idle"                                  # awg-bot reserve teardown
    svc.reserve_in_tick()
    assert svc.reserve_in() == "idle"


# ── без смены записи и путь с K основы — как прежде ─────────────────────────

def test_a_failure_before_the_record_change_ends_the_hosting_as_before(host):
    svc = host.svc
    _emergency(host, moved=False)
    host.zone.status = {"step": "fail", "reason": "DNS-запись: токен не принят"}
    acts = svc.reserve_in_tick()
    assert svc.reserve_in() == "idle" and "down" in sent(acts) and "teardown" in host.zone.names()
    assert all(not a[2].get("rec") for a in acts if a[:2] == ("send", "down")), "запись не менялась — вернуть нечего"


def test_on_the_key_path_the_record_step_is_not_a_record_change_of_mine(host):
    """По K основы запись меняет она: шаги record_* процесса тут ничего не значат — срыв прежний."""
    svc = host.svc
    _taking(host, "button")
    host.zone.status = {"step": "fail", "reason": "x", "record": "set"}
    acts = svc.reserve_in_tick()
    assert svc.reserve_in() == "idle" and "down" in sent(acts) and "teardown" in host.zone.names()


def test_a_new_opening_does_not_inherit_the_kept_episode(host):
    svc = host.svc
    svc.rsv_update("rsv_in", rec_moved=True, keep_k="record", keep_told="record", serve_due=True, relaunch=True)
    assert svc.reserve_go_req_in(7, "button") == ""
    inn = svc.rsv_get("rsv_in")
    assert not any(inn.get(k) for k in ("rec_moved", "keep_k", "keep_told", "serve_due", "relaunch")), inn


# ── клонов нет: перезапуск открытия, живая основа, вход потерян ─────────────

def _clones_gone(host) -> list[tuple]:
    host.zone.phase = "idle"                                  # клонов нет (сняты, хост перезагружался)
    host.zone.status = {"step": "fail", "reason": "зона: клон awg0 не поднялся", "record": "set", "name": NAME, "ip": IP}
    return host.svc.reserve_in_tick()


def test_without_clones_the_opening_is_relaunched_with_the_same_input_once_a_minute(host, monkeypatch):
    svc = host.svc
    _by_points(host, monkeypatch)
    acts = _clones_gone(host)
    inn = svc.rsv_get("rsv_in")
    assert svc.reserve_in() == "taking" and inn.get("relaunch") and inn.get("keep_k") == "relaunch", inn
    _no_teardown(host)
    assert "down" not in sent(acts)
    note = notices(acts)[0]
    assert note.force_sound and "интерфейс резерва не поднят" in note.text and "перезапускаю открытие раз в минуту" in note.text, note
    assert "└ ⚠️ интерфейс резерва не поднят: зона: клон awg0 не поднялся — перезапускаю открытие раз в минуту" in card_text(svc.reserve_card_data())
    host.clock.advance(inbound_keep.KEEP_RETRY_S - 5)
    assert svc.reserve_in_tick() == [] and len(host.spawn.popen) == 1, "перезапуск раньше срока"
    host.clock.advance(10)
    svc.reserve_in_tick()
    assert len(host.spawn.popen) == 2, "процесс открытия не перезапущен"
    assert payload_of(host.spawn.popen[1]) == payload_of(host.spawn.popen[0]), "перезапуск не тем же входом"
    assert payload_of(host.spawn.popen[1])["qpoints"] == POINTS["qpoints"]
    inn = svc.rsv_get("rsv_in")
    assert not inn.get("relaunch") and inn.get("relaunch_n") == 1 and inn.get("rec_moved"), inn
    # новый процесс снова не поднял клон — следующий перезапуск снова не раньше минуты, без снятия
    _clones_gone(host)
    svc.reserve_in_tick()
    assert len(host.spawn.popen) == 2 and svc.rsv_get("rsv_in").get("relaunch")
    host.clock.advance(inbound_keep.KEEP_RETRY_S + 1)
    svc.reserve_in_tick()
    assert len(host.spawn.popen) == 3
    _no_teardown(host)


def test_the_relaunch_comes_up_and_finishes_the_usual_way(host, monkeypatch):
    svc = host.svc
    _by_points(host, monkeypatch)
    _clones_gone(host)
    host.clock.advance(inbound_keep.KEEP_RETRY_S + 1)
    svc.reserve_in_tick()
    host.zone.phase = "taking"
    host.zone.status = {"step": "clone_up"}
    assert ("fw",) in svc.reserve_in_tick(), "файервол хоста не перерисован под клоны нового процесса"
    host.zone.status = {"step": "done"}
    svc.reserve_in_tick()
    assert "serve" in host.zone.names() and svc.reserve_in() == "taking", host.zone.calls
    assert svc.reserve_guest_tg_taken() and svc.reserve_in() == "serving"
    assert "_rsvopen_payload" not in svc.__dict__, "точки ключа кворума в памяти после приёма"


def test_the_relaunch_interval_is_jittered(host, monkeypatch):
    seen = []
    monkeypatch.setattr(backoff.random, "uniform", lambda a, b: seen.append((a, b)) or 1.4)
    _by_points(host, monkeypatch)
    _clones_gone(host)
    host.clock.advance(inbound_keep.KEEP_RETRY_S * 1.3)
    host.svc.reserve_in_tick()
    assert seen and seen[-1] == (0.6, 1.4) and len(host.spawn.popen) == 1, "строго периодический перезапуск"
    host.clock.advance(inbound_keep.KEEP_RETRY_S * 0.2)
    host.svc.reserve_in_tick()
    assert len(host.spawn.popen) == 2


def test_a_live_primary_after_the_failure_takes_its_users_back_instead(host, monkeypatch):
    svc = host.svc
    _by_points(host, monkeypatch)
    _clones_gone(host)
    monkeypatch.setattr(svc, "_peer_alive", lambda peer: True)
    svc.rsv_update("rsv_in", keep_at_ts=time.time() - 5)
    rm.st_in(svc)                                             # живой st основы после срыва
    host.clock.advance(inbound_keep.KEEP_RETRY_S + 1)
    acts = svc.reserve_in_tick()
    assert svc.reserve_in() == "idle" and len(host.spawn.popen) == 1, "перезапуск при живой основе"
    down = [a[2] for a in acts if a[:2] == ("send", "down")]
    assert down and down[0].get("rec") is True and "на связи" in down[0]["reason"], acts
    assert pairproto.check(pairproto.PAIR_PROTO, "down", dict(down[0]), pairproto.INVITER)["rec"] is True
    assert {"teardown", "wipe"} <= set(host.zone.names())


def test_a_st_from_before_the_failure_is_not_a_live_primary(host, monkeypatch):
    svc = host.svc
    _by_points(host, monkeypatch)
    rm.st_in(svc)                                             # st основы — до срыва
    _clones_gone(host)
    monkeypatch.setattr(svc, "_peer_alive", lambda peer: True)
    svc.rsv_update("rsv_in", keep_at_ts=time.time() + 5)
    host.clock.advance(inbound_keep.KEEP_RETRY_S + 1)
    svc.reserve_in_tick()
    assert len(host.spawn.popen) == 2 and svc.reserve_in() == "taking", "по старому st основа сочтена живой"


def test_a_lost_input_after_a_bot_restart_falls_back_to_the_old_failure_without_the_record(host, monkeypatch):
    svc = host.svc
    _by_points(host, monkeypatch)
    _clones_gone(host)
    svc.__dict__.pop("_rsvopen_payload", None)                # бот перезапущен — точек в памяти нет
    host.clock.advance(inbound_keep.KEEP_RETRY_S + 1)
    acts = svc.reserve_in_tick()
    assert svc.reserve_in() == "idle" and len(host.spawn.popen) == 1
    down = [a[2] for a in acts if a[:2] == ("send", "down")]
    assert down and not down[0].get("rec") and "открыть снова нечем" in down[0]["reason"], acts


# ── основа: уведомление по st.in_keep и запись на себя по down{rec} ──────────

def test_the_primary_tells_its_admin_once_per_episode(mv):
    keep = {"k": "record", "why": "факт записи не подтвердился за 30 минут", "at": "2026-11-01T10:00:00+03:00"}
    assert pairproto.check(pairproto.PAIR_PROTO, "st", rm.peer_st(**{"in": "serving", "in_keep": keep}),
                           pairproto.ACCEPTOR)["in_keep"] == keep
    notes = rm.st_in(mv.svc, **{"in": "serving", "in_keep": keep})
    loud = [n for n in notes if "VPN работает" in n.text]
    assert len(loud) == 1 and loud[0].force_sound and "30 минут" in loud[0].text and "«Beta» повторяет сам" in loud[0].text
    assert not [n for n in rm.st_in(mv.svc, **{"in": "serving", "in_keep": keep}) if "VPN работает" in n.text]
    notes = rm.st_in(mv.svc, **{"in": "taking", "in_keep": {**keep, "k": "relaunch", "at": "2026-11-01T10:05:00+03:00"}})
    assert [n for n in notes if "интерфейс резерва там не поднят" in n.text], "новый эпизод не сказан"


def _home_rec_back(mv, monkeypatch) -> None:
    rm.automatic(mv.svc)
    monkeypatch.setattr(mv.svc, "_peer_alive", lambda peer: True)
    assert mv.svc.reserve_down_in("клон не поднят — «Alpha» на связи", rec=True) == []
    assert mv.dns.sets == [], "запись по одному слову, без живого st после него"


def test_down_with_rec_returns_the_record_on_a_live_st_after_it(mv, monkeypatch):
    svc = mv.svc
    _home_rec_back(mv, monkeypatch)
    svc.rsv_update("rsv_peer_state", at_ts=time.time() - 10)  # st резерва — до слова
    svc.reserve_out_tick()
    assert mv.dns.sets == []
    rm.st_in(svc, **{"in": "idle"})
    acts = svc.reserve_out_tick()
    assert mv.dns.sets == [OWN_IP], mv.dns.sets
    note = [n for n in notices(acts) if "↩️" in n.text][0]
    assert note.force_sound and rc.HOST_A in note.text and OWN_IP in note.text and "«Beta» не принял" in note.text
    svc.reserve_out_tick()
    assert mv.dns.sets == [OWN_IP], "запись возвращена дважды"


def test_a_reserve_hosting_again_keeps_the_record(mv, monkeypatch):
    svc = mv.svc
    _home_rec_back(mv, monkeypatch)
    rm.st_in(svc, **{"in": "taking"})                        # кворум открыл снова — запись его
    svc.reserve_out_tick()
    rm.st_in(svc, **{"in": "idle"})
    svc.reserve_out_tick()
    assert mv.dns.sets == [] and not svc.rsv_get("rsv_out").get("rec_back")


def test_without_a_live_reserve_the_record_is_not_touched(mv, monkeypatch):
    svc = mv.svc
    _home_rec_back(mv, monkeypatch)
    monkeypatch.setattr(svc, "_peer_alive", lambda peer: False)
    rm.st_in(svc, **{"in": "idle"})
    svc.reserve_out_tick()
    assert mv.dns.sets == [] and svc.rsv_get("rsv_out").get("rec_back"), "отметка снята без живого слова"


def test_a_manual_record_is_asked_from_the_admin(mv, monkeypatch):
    svc = mv.svc
    monkeypatch.setattr(svc, "_peer_alive", lambda peer: True)
    svc.reserve_down_in("клон не поднят", rec=True)
    rm.st_in(svc, **{"in": "idle"})
    acts = svc.reserve_out_tick()
    assert mv.dns.sets == [] and any("смени у провайдера" in n.text for n in notices(acts)), acts


def test_down_with_rec_during_a_button_move_is_the_move_failure_as_before(mv):
    svc = mv.svc
    rm.to_step(svc, "up")
    svc.reserve_down_in("порт занят", rec=True)
    assert svc.reserve_out() == "home" and not svc.rsv_get("rsv_out").get("rec_back")

