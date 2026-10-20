"""Переезд кнопкой на стороне основы (domain/services/reserve/outbound.py):
проверка возможности, шаги pkg → up → record → release → released → away,
сроки шагов, отказы и события канала.

Цена ошибки: переезд, объявленный до того, как запись DNS указывает на
резерв, — клиенты остаются на основе без бота, а бот уже у резерва;
`release` до факта записи — поллинг остановлен, а ответить в Telegram
некому; зависший шаг без `back` резерву — клон и вебхук остаются у резерва
навсегда; второе `taken` с тем же уведомлением — админ видит «перевёл» дважды;
переезд, начатый с недоступным резервом, — человек ждёт, а `go` никуда не ушёл.
"""
from __future__ import annotations

import pytest

from awgbot.core import settings
from awgbot.domain.services.reserve import outbound
from awgbot.domain.services.types import ServiceError
from awgbot.util import pairproto, timeutil
from awgbot.util.rsvpkg import bot as pkgbot, seal

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import OWN_IP, PEER_IP, actions_of, notices, sent

pytestmark = pytest.mark.integration
ADMIN_CHAT = rc.ADMIN
rsv = rc.rsv
mv = rm.mv


# ── можно ли переехать ───────────────────────────────────────────────────────

def test_the_move_is_possible_for_a_ready_pair_and_says_nothing(mv):
    assert mv.svc.reserve_move_check() == ""


def test_every_obstacle_gives_its_own_reason_and_no_start(mv):
    svc = mv.svc
    svc.rsv_update("rsv_peer_state", session=False)
    assert "не на связи" in svc.reserve_move_check()
    with pytest.raises(ServiceError):
        svc.reserve_move_start()
    assert svc.reserve_out() == "home", "отказанный старт не должен менять состояние"
    svc.rsv_update("rsv_peer_state", session=True)
    rm.st_in(svc, ready=False)
    assert "не готов" in svc.reserve_move_check()
    rm.st_in(svc)
    settings.set_value("app.network.server_host", "203.0.113.5")
    assert "записи DNS нет" in svc.reserve_move_check(), "адрес для клиентов — IP: менять нечего"
    settings.set_value("app.network.server_host", rc.HOST_A)
    svc.rsv_update("rsv_in", state="serving")
    assert "возврат" in svc.reserve_move_check(), "у меня клиенты резерва — переезд поверх них невозможен"
    svc.rsv_put("rsv_in", None)
    svc.reserve_set_out("away")
    assert "уже не на этом сервере" in svc.reserve_move_check()


def test_without_a_pair_the_move_check_says_there_is_no_reserve(services, rsv):
    assert services.reserve_move_check() == "резерва нет"


def test_the_move_starts_with_a_fresh_package_request_and_remembers_the_target(mv):
    svc = mv.svc
    svc.__dict__.pop("_rsv_pkg_due_at", None)
    svc.reserve_move_start(chat_id=ADMIN_CHAT, message_id=55)
    out = svc.rsv_get("rsv_out")
    assert out["state"] == "leaving" and out["step"] == "pkg" and out["kind"] == "button"
    assert out["target_ip"] == PEER_IP and out["record"] == rc.HOST_A and out["msg"] == [ADMIN_CHAT, 55]
    assert svc.__dict__.get("_rsv_pkg_due_at") is not None, "свежий пакет вне очереди не запрошен"
    assert svc.reserve_busy(), "идущий переезд обязан считаться занятостью — иначе рядом стартует второй"


# ── pkg → up ────────────────────────────────────────────────────────────────

def test_go_waits_for_a_package_confirmed_after_the_start_and_carries_the_key(mv):
    svc = mv.svc
    rm.confirm_package(svc, gen=3)                       # пакет подтверждён ДО старта
    svc.rsv_update("rsv_pkg_sent", at=timeutil.to_iso(timeutil.shift(timeutil.now(), -3600)))
    svc.reserve_move_start()
    assert svc.reserve_out_tick() == [], "старый пакет принят за свежий — резерв поднимет клиентов по прежним данным"
    rm.confirm_package(svc, gen=4)
    acts = svc.reserve_out_tick()
    go = [a for a in acts if a[:2] == ("send", "go")]
    assert len(go) == 1
    body = go[0][2]
    assert body == {"k": svc.reserve_k().hex(), "gen": 4, "kind": "button"}
    pairproto.check(pairproto.PAIR_PROTO, "go", body, pairproto.INVITER)
    assert svc.rsv_get("rsv_out")["step"] == "up"
    assert svc.reserve_out_tick() == [], "go ушёл один раз: на шаге up такт молчит"


def test_up_from_the_reserve_moves_to_the_record_step_once(mv):
    svc = mv.svc
    rm.to_step(svc, "up")
    assert svc.reserve_up_in() == [("live",)]
    assert svc.rsv_get("rsv_out")["step"] == "record"
    assert svc.reserve_up_in() == [], "повторное up не двигает автомат"


def test_up_outside_a_move_is_ignored(mv):
    assert mv.svc.reserve_up_in() == [] and mv.svc.reserve_out() == "home"


# ── record: запись — факт, не отметка ───────────────────────────────────────

def test_the_move_is_not_announced_until_the_record_points_to_the_reserve(mv):
    """Ключевое: пока авторитетный сервер отвечает прежним адресом, ни `release`, ни away."""
    svc = mv.svc
    rm.to_step(svc, "record")
    for _ in range(5):
        mv.clock.advance(30)
        acts = svc.reserve_out_tick()
        assert not actions_of(acts, "release"), acts
        assert svc.reserve_out() == "leaving"
    assert "жду смену записи" in svc.reserve_main_line()
    mv.dns.auth = [PEER_IP]
    mv.clock.advance(30)
    acts = svc.reserve_out_tick()
    assert ("release",) in acts and svc.rsv_get("rsv_out")["step"] == "release"
    assert svc.reserve_out() == "leaving", "release — ещё не away: право отвечать передаётся только по taken"


def test_manual_mode_never_calls_the_provider_and_waits_for_the_human(mv):
    svc = mv.svc
    rm.to_step(svc, "record")
    mv.clock.advance(30)
    svc.reserve_out_tick()
    assert mv.dns.sets == [], "без токена бот не правит запись"
    assert svc.rsv_get("rsv_out")["record_set_at"], "время «ждём человека» не отмечено"


def test_automatic_mode_changes_the_record_exactly_once(mv):
    svc = mv.svc
    rm.automatic(svc)
    rm.to_step(svc, "record")
    for _ in range(4):
        mv.clock.advance(30)
        svc.reserve_out_tick()
    assert mv.dns.sets == [PEER_IP], mv.dns.sets


def test_the_authoritative_server_is_not_polled_more_often_than_every_20_seconds(mv):
    svc = mv.svc
    rm.to_step(svc, "record")
    mv.clock.advance(30)
    svc.reserve_out_tick()
    reads = mv.dns.auth_reads
    for _ in range(5):
        mv.clock.advance(3)
        svc.reserve_out_tick()
    assert mv.dns.auth_reads == reads, "каждые 3 с — запрос к чужому серверу: слишком часто и похоже на маячок"
    mv.clock.advance(10)
    svc.reserve_out_tick()
    assert mv.dns.auth_reads == reads + 1


def test_a_failing_provider_during_a_move_aborts_it_and_releases_the_reserve(mv):
    svc = mv.svc
    rm.automatic(svc)
    mv.dns.set_error = "токен отозван"
    rm.to_step(svc, "record")
    mv.clock.advance(30)
    acts = svc.reserve_out_tick()
    assert svc.reserve_out() == "home"
    assert sent(acts) == ["back"], "резерв поднял клон — без back он останется у него"
    text = " ".join(n.text for n in notices(acts))
    assert "Перевод не состоялся" in text and "токен отозван" in text and "отпустил клиентов" in text


def test_an_unreadable_authoritative_server_is_not_a_fact(mv):
    svc = mv.svc
    rm.to_step(svc, "record")
    mv.dns.auth_error = True
    mv.clock.advance(30)
    assert not actions_of(svc.reserve_out_tick(), "release")
    assert svc.rsv_get("rsv_out")["step"] == "record"


# ── сроки шагов ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("step,limit,back,reason", [
    ("pkg", outbound.PKG_WAIT_S, False, "не подтверждены"),
    ("up", outbound.UP_WAIT_S, True, "не поднял клиентов"),
    ("record", outbound.RECORD_WAIT_S, True, "30 минут"),
    ("release", outbound.RELEASED_WAIT_S, True, "передача бота"),
])
def test_a_step_that_outlives_its_deadline_fails_the_move_and_releases_the_reserve_when_the_reserve_may_hold_a_clone(
        mv, step, limit, back, reason):
    svc = mv.svc
    rm.to_step(svc, step, wait=limit)
    mv.clock.advance(limit - 1)
    assert not notices(svc.reserve_out_tick()) and svc.reserve_out() == "leaving", "срок ещё не вышел"
    mv.clock.advance(2)
    acts = svc.reserve_out_tick()
    assert svc.reserve_out() == "home", "по сроку шаг обязан закончиться откатом"
    assert (sent(acts) == ["back"]) is back, f"{step}: back ожидался={back}, ушло {sent(acts)}"
    assert reason in " ".join(n.text for n in notices(acts))


def test_the_released_step_that_outlives_its_deadline_counts_as_away_because_the_webhook_decides(mv):
    """`taken` не пришёл, но бот уже отдан: право отвечать — у вебхука, откат был бы ложью."""
    svc = mv.svc
    rm.to_step(svc, "released")
    mv.clock.advance(outbound.RELEASED_WAIT_S + 1)
    svc.reserve_out_tick()
    assert svc.reserve_out() == "away" and svc.reserve_poll() == "away"


def test_down_from_the_reserve_aborts_the_move_without_a_second_back(mv):
    svc = mv.svc
    rm.to_step(svc, "up")
    acts = svc.reserve_down_in("порт занят")
    assert svc.reserve_out() == "home" and not sent(acts), "резерв сам снял зону: back ему не нужен"
    text = " ".join(n.text for n in notices(acts))
    assert "порт занят" in text and "отпустил" not in text
    assert svc.reserve_down_in("ещё раз") == [], "дома down — просто шум"


# ── release: финальная реплика ──────────────────────────────────────────────

def test_the_final_replica_is_built_only_after_the_record_fact_and_opens_under_k(mv):
    svc = mv.svc
    assert svc.reserve_release_build() is None, "дома реплики для передачи нет"
    rm.to_step(svc, "record")
    assert svc.reserve_release_build() is None, "до факта записи реплика не собирается"
    rm.to_step(svc, "release", gen=9)
    built = svc.reserve_release_build()
    assert built["gen"] == 9
    snap = seal.snapshot_open(svc.reserve_k(), bytes.fromhex(built["blob"]))
    assert snap["pkg_gen"] == 9 and snap["db"], "реплика пуста"
    pkgbot.assert_clean(bytes.fromhex(snap["db"]))
    assert svc.rsv_get("rsv_out")["step"] == "released" and svc.reserve_poll() == "away"
    assert svc.reserve_release_build() is None, "второй вызов не собирает вторую реплику"


def test_a_replica_that_fails_the_guard_is_not_sent(mv, monkeypatch):
    svc = mv.svc
    rm.to_step(svc, "release")

    def bad(_b):
        raise pkgbot.BotPartError("в реплике лишний ключ состояния: rsv_out")
    monkeypatch.setattr(pkgbot, "assert_clean", bad)
    assert svc.reserve_release_build() is None
    assert svc.rsv_get("rsv_out")["step"] == "release" and svc.reserve_poll() == "mine", \
        "сторож не пропустил реплику, а поллинг уже отдан"


def test_another_k_does_not_open_the_replica(mv):
    svc = mv.svc
    rm.to_step(svc, "release")
    blob = bytes.fromhex(svc.reserve_release_build()["blob"])
    with pytest.raises(seal.PackageError):
        seal.snapshot_open(b"\x01" * 32, blob)


# ── taken ───────────────────────────────────────────────────────────────────

def test_taken_makes_the_clients_away_and_tells_the_admin_once(mv):
    svc = mv.svc
    rm.to_step(svc, "released")
    acts = svc.reserve_taken_in()
    out = svc.rsv_get("rsv_out")
    assert out["state"] == "away" and out["kind"] == "button" and svc.reserve_poll() == "away"
    assert out["last_switch"] and out["taken_at"]
    texts = [n.text for n in notices(acts)]
    assert len(texts) == 1
    assert "Beta" in texts[0] and "переподключи VPN" in texts[0]
    assert not texts[0].rstrip().endswith("."), "уведомления без точки в конце"
    assert svc.reserve_taken_in() == [], "второе taken не должно слать второе уведомление"


def test_taken_outside_a_move_changes_nothing(mv):
    assert mv.svc.reserve_taken_in() == [] and mv.svc.reserve_out() == "home" and mv.svc.reserve_poll() == "mine"


# ── клиенты у резерва мимо меня ─────────────────────────────────────────────

@pytest.mark.parametrize("inn", ["taking", "serving"])
def test_when_the_reserve_reports_my_clients_at_its_place_the_primary_becomes_away(mv, inn):
    svc = mv.svc
    rm.st_in(svc, **{"in": inn})
    acts = svc.reserve_out_tick()
    assert svc.reserve_out() == "away" and svc.reserve_poll() == "away" and acts == [("live",)]
    assert svc.rsv_get("rsv_out")["kind"] == "dead"


def test_the_idle_reserve_does_not_make_the_primary_away(mv):
    rm.st_in(mv.svc, **{"in": "idle"})
    assert mv.svc.reserve_out_tick() == [] and mv.svc.reserve_out() == "home"


def test_an_offline_reserve_does_not_move_the_primary_away_by_a_stale_st(mv):
    svc = mv.svc
    rm.st_in(svc, **{"in": "serving"})
    svc.rsv_update("rsv_peer_state", session=False)
    assert svc.reserve_out_tick() == [] and svc.reserve_out() == "home"


def test_the_out_tick_without_a_pair_does_nothing(services, rsv):
    assert services.reserve_out_tick() == []


# ── живое сообщение хода ────────────────────────────────────────────────────

def test_the_live_view_follows_the_steps_and_marks_manual_mode(mv):
    svc = mv.svc
    rm.to_step(svc, "record", kind="button")
    v = svc.reserve_move_live()
    assert v["state"] == "leaving" and v["step"] == "record" and v["manual"] is True
    assert v["target"] == PEER_IP and v["record"] == rc.HOST_A and v["name"] == "Beta"
    rm.automatic(svc)
    assert svc.reserve_move_live()["manual"] is False


def test_the_move_dialog_data_names_the_record_target_and_mode(mv):
    d = mv.svc.reserve_move_dialog_data()
    assert d == {"name": "Beta", "record": rc.HOST_A, "target": PEER_IP, "manual": True, "own": OWN_IP}


def test_an_old_confirmed_package_blocks_the_move_start(mv):
    """Пакет, подтверждённый больше двух часов назад, — данные резерва устарели: кнопка отказывает."""
    svc = mv.svc
    svc.rsv_put("rsv_pkg_sent", {"gen": 3, "fp": "DUMMY-fp", "at": timeutil.to_iso(timeutil.shift(timeutil.now(), -3 * 3600))})
    assert "устарели" in svc.reserve_move_check()


def test_a_move_that_failed_after_the_record_was_switched_returns_the_record_to_the_primary(mv):
    """Запись уже указала на резерв, а передача бота не состоялась: без возврата записи клиенты
    уходят на сервер, где нет ни клона, ни бота."""
    svc = mv.svc
    rm.automatic(svc)
    rm.to_step(svc, "record")
    mv.clock.advance(30)
    svc.reserve_out_tick()
    assert mv.dns.sets == [PEER_IP]
    mv.dns.auth = [PEER_IP]
    mv.clock.advance(30)
    svc.reserve_out_tick()                                # шаг release
    mv.clock.advance(outbound.RELEASED_WAIT_S + 1)
    acts = svc.reserve_out_tick()
    assert svc.reserve_out() == "home" and sent(acts) == ["back"]
    assert mv.dns.sets == [PEER_IP, OWN_IP], mv.dns.sets


def test_a_failed_move_with_a_manual_record_leaves_the_provider_alone(mv):
    svc = mv.svc
    rm.to_step(svc, "record")
    mv.clock.advance(30)
    svc.reserve_out_tick()
    mv.clock.advance(outbound.RECORD_WAIT_S + 1)
    svc.reserve_out_tick()
    assert mv.dns.sets == [] and svc.reserve_out() == "home"


def test_an_st_from_the_reserve_does_not_end_the_live_session(services, rsv):
    """После первого `st` по живой сессии второй сервер «не на связи»: переезд, возврат и
    экраны пары отказывают, хотя канал работает. Приём `st` обязан менять поля, а не заменять
    состояние пары целиком."""
    rc.pair_row(services)
    services.reserve_session_opened({})
    assert services.reserve_online()
    services.reserve_st_in(rm.peer_st())
    assert services.reserve_online(), services.reserve_peer_state()


def test_in_kind_of_the_reserve_lands_in_the_peer_state_and_decides_the_kind_of_my_away(mv):
    svc = mv.svc
    rm.st_in(svc, **{"in": "serving", "in_kind": "rf"})
    assert svc.reserve_peer_state()["in_kind"] == "rf"
    svc.reserve_out_tick()
    assert svc.rsv_get("rsv_out")["kind"] == "rf", "вид переезда у резерва не дошёл до основы"
