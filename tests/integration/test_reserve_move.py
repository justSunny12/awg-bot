"""Переезд кнопкой на стороне основы (domain/services/reserve/outbound.py):
проверка возможности, шаги pkg → eph → up → record → release → released →
away, ключ пакета конвертом к разовому ключу процесса открытия резерва
(`go_req` → `open_eph` → `go{sealed, oid}`), сроки шагов, отказы и события
канала.

Цена ошибки: переезд, объявленный до того, как запись DNS указывает на
резерв, — пользователи остаются на основе без бота, а бот уже у резерва;
`release` до факта записи — поллинг остановлен, а ответить в Telegram
некому; зависший шаг без `back` резерву — клон и вебхук остаются у резерва
навсегда; второе `taken` с тем же уведомлением — админ видит «перевёл» дважды;
переезд, начатый с недоступным резервом, — человек ждёт, а `go` никуда не ушёл;
ключ пакета открытым в канале — бот резерва (а с ним чужой администратор)
может снять пакет основы целиком; конверт к чужому разовому ключу — ключ
уходит не тому процессу.
"""
from __future__ import annotations

import pytest

from awgbot.core import settings
from awgbot.domain.services.reserve import compat, outbound
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
    assert "у «Beta» выключено «Готов быть резервом»" in svc.reserve_move_check(), "причина не называет сторону"
    rm.st_in(svc)
    settings.set_value("app.network.server_host", "203.0.113.5")
    assert "записи DNS нет" in svc.reserve_move_check(), "адрес для пользователей — IP: менять нечего"
    settings.set_value("app.network.server_host", rc.HOST_A)
    svc.rsv_update("rsv_in", state="serving")
    assert "возврат" in svc.reserve_move_check(), "у меня пользователи резерва — переезд поверх них невозможен"
    svc.rsv_put("rsv_in", None)
    svc.reserve_set_out("away")
    assert "уже не на этом сервере" in svc.reserve_move_check()


def test_a_reserve_that_cannot_take_the_key_sealed_gets_no_move_button(mv):
    """Резерв без `seal1` принял бы ключ только открытым — переезд не
    предлагается вовсе, без запасного пути с открытым `k`."""
    svc = mv.svc
    rm.st_in(svc, caps=[c for c in compat.CAPS if c != "seal1"])
    why = svc.reserve_move_check()
    assert "зашифрованный ключ" in why and "Beta" in why and "v4.0.2" in why, why
    with pytest.raises(ServiceError):
        svc.reserve_move_start()
    assert svc.reserve_out() == "home"
    rm.st_in(svc)
    assert svc.reserve_move_check() == ""


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


# ── pkg → eph → up ──────────────────────────────────────────────────────────

def test_the_package_tick_builds_the_fresh_package_of_a_move_and_its_ack_leads_to_go_req(mv):
    """Настоящий путь шага `pkg`: старт переезда ставит отметку, такт пакета
    собирает пакет (основа уже в `leaving`), резерв подтверждает — и автомат
    просит разовый ключ. Если такт пакета молчит в `leaving`, свежего пакета
    нет никогда, и каждый переезд кнопкой срывается через PKG_WAIT_S с
    «данные для переезда не подтверждены резервом»."""
    svc = mv.svc
    svc.reserve_move_start(chat_id=ADMIN_CHAT, message_id=55)
    assert svc.reserve_out_tick() == [], "go_req ушёл без пакета, подтверждённого после старта"
    mv.clock.advance(11)                                  # пачка правок закончилась
    built = svc.reserve_pkg_tick()
    assert built is not None, "в leaving на шаге pkg такт пакета промолчал — переезд сорвётся по сроку"
    assert seal.open_parts(bytes.fromhex(built["blob"]), svc.reserve_k()), "пакет не читается ключом пакета"
    assert svc.reserve_pkg_tick() is None, "пакет ещё в пути — второй такт собрал второй"
    svc.reserve_pkg_ok(built["gen"], built["fp"])        # резерв принял части и ответил pkg_ok
    acts = svc.reserve_out_tick()
    assert sent(acts) == ["go_req"], f"подтверждённый пакет не перевёл переезд к разовому ключу: {sent(acts)}"
    assert actions_of(acts, "send")[0][2]["gen"] == built["gen"], "go_req называет не тот пакет, что подтверждён"
    out = svc.rsv_get("rsv_out")
    assert out["state"] == "leaving" and out["step"] == "eph", out


@pytest.mark.parametrize("step", ["eph", "up", "record", "release", "released"])
def test_after_the_pkg_step_of_a_move_the_package_tick_stays_silent(mv, step):
    """После шага `pkg` пакет уже у резерва и K ушёл (или уходит) к его процессу
    открытия: новый пакет посреди переезда — гонка с открытием прежнего и
    лишний пакет при передаче бота."""
    svc = mv.svc
    rm.to_step(svc, step)
    svc.reserve_pkg_mark()
    mv.clock.advance(11)
    assert svc.reserve_pkg_tick() is None, f"на шаге {step} переезда такт собрал пакет"


@pytest.mark.parametrize("state, step", [("away", ""), ("returning", "back_req"), ("returning", "merge")])
def test_while_the_clients_are_away_or_returning_the_package_tick_stays_silent(mv, state, step):
    """Пользователи у резерва или возвращаются: данные ведёт бот основы на
    резерве, пакет по устаревшей базе основы резерву не нужен."""
    svc = mv.svc
    svc.reserve_set_out(state, kind="button", gen=7, step=step, taken_at=timeutil.now_iso())
    svc.reserve_pkg_mark()
    mv.clock.advance(11)
    assert svc.reserve_pkg_tick() is None, f"в {state}/{step} такт собрал пакет"


def test_go_req_waits_for_a_package_confirmed_after_the_start_and_carries_no_key(mv):
    svc = mv.svc
    rm.confirm_package(svc, gen=3)                       # пакет подтверждён ДО старта
    svc.rsv_update("rsv_pkg_sent", at=timeutil.to_iso(timeutil.shift(timeutil.now(), -3600)))
    svc.reserve_move_start()
    assert svc.reserve_out_tick() == [], "старый пакет принят за свежий — резерв поднимет пользователей по прежним данным"
    rm.confirm_package(svc, gen=4)
    acts = svc.reserve_out_tick()
    assert sent(acts) == ["go_req"], f"после пакета ждём просьбу о разовом ключе, ушло: {sent(acts)}"
    body = actions_of(acts, "send")[0][2]
    assert body == {"gen": 4, "kind": "button", "ttl": 0}, "TTL записи ещё не читали — 0; ключа в go_req нет"
    pairproto.check(pairproto.PAIR_PROTO, "go_req", body, pairproto.INVITER)
    assert svc.rsv_get("rsv_out")["step"] == "eph"
    assert svc.reserve_out_tick() == [], "go_req ушёл один раз: на шаге eph такт молчит"
    assert svc.reserve_k().hex() not in " ".join(str(a) for a in acts), "ключ пакета ушёл в канал открытым"


def test_the_ephemeral_key_of_the_reserve_turns_into_go_with_the_key_sealed_to_it(mv):
    """`open_eph` от резерва → `go{sealed, oid}`: конверт открывается только
    закрытой частью процесса открытия, внутри — K, поколение и oid; шаг up."""
    svc = mv.svc
    svc.rsv_update("rsv_dns", ttl=900)
    svc.reserve_move_start()
    rm.confirm_package(svc, gen=4)
    svc.reserve_out_tick()
    priv, acts = rm.open_eph(svc, gen=4)
    assert sent(acts) == ["go"] and ("live",) in acts, acts
    body = actions_of(acts, "send")[0][2]
    pairproto.check(pairproto.PAIR_PROTO, "go", body, pairproto.INVITER)
    assert set(body) == {"gen", "kind", "ttl", "sealed", "oid"}, f"в go лишние или недостающие поля: {sorted(body)}"
    assert body["gen"] == 4 and body["kind"] == "button" and body["ttl"] == 900 and body["oid"] == "0123456789abcdef"
    assert "k" not in body and svc.reserve_k().hex() not in body["sealed"], "ключ пакета в go открытым"
    assert seal.open_sealed_k(priv, bytes.fromhex(body["sealed"]), 4, "0123456789abcdef") == svc.reserve_k(), \
        "конверт не открывается разовой парой резерва — процесс открытия ключа не получит"
    out = svc.rsv_get("rsv_out")
    assert out["step"] == "up" and out["go_sent"] is True and out["oid"] == "0123456789abcdef"
    assert svc.reserve_k().hex() not in str(out) and body["sealed"] not in str(out), "ключ или конверт осели в rsv_out"
    assert svc.reserve_out_tick() == [], "go ушёл один раз: на шаге up такт молчит"


def test_a_repeated_or_foreign_open_eph_is_ignored(mv):
    """Чужое поколение, другой процесс открытия (другой `oid`) или не тот шаг — молча:
    второго `go` нет. Повтор того же процесса на шаге up — `go` снова (прежний
    потерялся: резерв повторяет `open_eph`, только пока процесс ждёт конверт)."""
    svc = mv.svc
    svc.reserve_move_start()
    rm.confirm_package(svc, gen=4)
    svc.reserve_out_tick()
    _priv, acts = rm.open_eph(svc, gen=5)
    assert acts == [] and svc.rsv_get("rsv_out")["step"] == "eph", "чужое поколение дало go"
    _priv, acts = rm.open_eph(svc, gen=4)
    assert sent(acts) == ["go"]
    _priv, again = rm.open_eph(svc, gen=4, oid="ffffffffffffffff")
    assert again == [] and svc.rsv_get("rsv_out")["step"] == "up", "повтор open_eph дал второй go — два конверта к разным ключам"
    assert svc.reserve_out() == "leaving"
    deadline = svc.rsv_get("rsv_out")["deadline"]
    priv, again = rm.open_eph(svc, gen=4)
    assert sent(again) == ["go"], "потерянный go не повторён — срыв переезда через UP_WAIT_S"
    body = actions_of(again, "send")[0][2]
    assert seal.open_sealed_k(priv, bytes.fromhex(body["sealed"]), 4, "0123456789abcdef") == svc.reserve_k()
    out = svc.rsv_get("rsv_out")
    assert out["step"] == "up" and out["deadline"] == deadline, "повтор go продлил срок шага up"
    svc._out_step("record", 300)
    assert rm.open_eph(svc, gen=4)[1] == [], "open_eph вне шагов eph и up что-то сделал"


def test_an_open_eph_with_a_malformed_key_fails_the_move_and_releases_the_reserve(mv):
    svc = mv.svc
    svc.reserve_move_start()
    rm.confirm_package(svc, gen=4)
    svc.reserve_out_tick()
    k = svc.reserve_k()
    acts = svc.reserve_open_eph_in(4, "zz", "0123456789abcdef")
    assert svc.reserve_out() == "home" and sent(acts) == ["back"], "резерв держит процесс открытия — без back он ждёт 2 минуты впустую"
    assert "не годится" in " ".join(n.text for n in notices(acts))
    assert svc.reserve_k() == k, "K не уходил — менять его незачем"
    acts = svc.reserve_open_eph_in(4, "cd" * 16, "0123456789abcdef")
    assert acts == [], "вне переезда open_eph что-то сделал"


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
    assert "Перевод не состоялся" in text and "токен отозван" in text and "отпустил профили" in text


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
    ("eph", outbound.EPH_WAIT_S, True, "разовый ключ от «Beta» не пришёл"),
    ("up", outbound.UP_WAIT_S, True, "не поднял профили"),
    ("record", outbound.RECORD_WAIT_S, True, "30 минут"),
    ("release", outbound.RELEASED_WAIT_S, True, "передача бота"),
])
def test_a_step_that_outlives_its_deadline_fails_the_move_and_releases_the_reserve_when_the_reserve_may_hold_a_clone(
        mv, step, limit, back, reason):
    svc = mv.svc
    rm.to_step(svc, step, wait=limit)
    mv.clock.advance(limit - 1)
    early = " ".join(n.text for n in notices(svc.reserve_out_tick()))      # на шаге записи — просьба сменить запись руками
    assert reason not in early and "не состоялся" not in early and svc.reserve_out() == "leaving", "срок ещё не вышел"
    mv.clock.advance(2)
    acts = svc.reserve_out_tick()
    assert svc.reserve_out() == "home", "по сроку шаг обязан закончиться откатом"
    assert (sent(acts) == ["back"]) is back, f"{step}: back ожидался={back}, ушло {sent(acts)}"
    assert reason in " ".join(n.text for n in notices(acts))


@pytest.mark.parametrize("why", ["webhook", "webhook_info"])
@pytest.mark.parametrize("kind", ["button", "rf"])
def test_the_released_step_that_outlives_its_deadline_counts_as_away_when_the_webhook_is_at_the_reserve(mv, kind, why):
    """`taken` не пришёл, но Telegram (после released) говорит, что вебхук у
    резерва: откат был бы ложью — away и уведомление о переезде; вид переезда
    сохраняется (от него зависит поколение после возврата), отметка
    переключения ставится (демпфер возврата)."""
    svc = mv.svc
    rm.to_step(svc, "released", kind=kind)
    svc.reserve_set_poll("away", why=why)
    mv.clock.advance(outbound.RELEASED_WAIT_S + 1)
    acts = svc.reserve_out_tick()
    out = svc.rsv_get("rsv_out")
    assert svc.reserve_out() == "away" and out["kind"] == kind, out
    assert len(notices(acts)) == 1 and "Beta" in notices(acts)[0].text, f"о переезде не сказано: {acts}"
    assert out.get("last_switch"), "переключение без отметки — демпфер возврата не сработает"


def test_my_own_released_mark_is_not_the_word_of_telegram(mv):
    """Отметка away, которую основа ставит себе при released (why=released), —
    не слово Telegram: по сроку — проверка, а не away вслепую."""
    svc = mv.svc
    rm.to_step(svc, "released")
    svc.reserve_set_poll("away", why="released")
    mv.clock.advance(outbound.RELEASED_WAIT_S + 1)
    assert svc.reserve_out_tick() == [("wake_poll",)] and svc.reserve_out() == "leaving"


def test_a_word_of_telegram_from_before_released_does_not_count(mv):
    svc = mv.svc
    svc.reserve_set_poll("away", why="webhook")                 # слово прошлого переезда
    svc.rsv_update("rsv_poll", since="2026-01-01T00:00:00+00:00")
    rm.to_step(svc, "released")
    svc.rsv_update("rsv_out", released_at=timeutil.now_iso())
    mv.clock.advance(outbound.RELEASED_WAIT_S + 1)
    acts = svc.reserve_out_tick()
    assert acts == [("wake_poll",)] and svc.reserve_out() == "leaving", f"старое слово Telegram сочтено за переезд: {acts}"


def test_the_released_step_with_telegram_saying_mine_fails_the_move(mv):
    svc = mv.svc
    rm.to_step(svc, "released")
    mv.clock.advance(outbound.RELEASED_WAIT_S + 1)
    # как pollctl: getWebhookInfo сперва показал вебхук у резерва, потом вебхука не стало — mine без причины
    svc.reserve_set_poll("away", why="webhook_info", url="https://203.0.113.5:8443/DUMMY")
    svc.reserve_set_poll("mine")
    acts = svc.reserve_out_tick()
    assert svc.reserve_out() == "home" and "back" in sent(acts), acts
    assert "вебхук не поставлен" in " ".join(n.text for n in notices(acts))


def test_the_released_step_without_a_word_of_telegram_wakes_the_poll_three_times_and_then_fails(mv):
    """Слова Telegram нет: до трёх проверок getWebhookInfo через 120 с, затем
    переезд сорван с причиной «право отвечать не проверено»."""
    svc = mv.svc
    rm.to_step(svc, "released")
    mv.clock.advance(outbound.RELEASED_WAIT_S + 1)
    for i in range(3):
        acts = svc.reserve_out_tick()
        assert acts == [("wake_poll",)] and svc.reserve_out() == "leaving", f"проверка {i + 1}: {acts}"
        mv.clock.advance(100)
        assert svc.reserve_out_tick() == [], "следующая проверка раньше 120 с"
        mv.clock.advance(30)
    acts = svc.reserve_out_tick()
    assert svc.reserve_out() == "home" and "back" in sent(acts), acts
    assert "право отвечать не проверено" in " ".join(n.text for n in notices(acts))


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


# ── пользователи у резерва мимо меня ────────────────────────────────────────

def test_taking_at_the_reserve_is_not_yet_a_fact_that_my_clients_moved(mv):
    """taking — резерв только открывает пакет: клон может не встать, основа
    остаётся дома, пока резерв не скажет serving."""
    svc = mv.svc
    rm.st_in(svc, **{"in": "taking"})
    assert svc.reserve_out_tick() == [] and svc.reserve_out() == "home" and svc.reserve_poll() != "away"


def test_a_serving_st_from_before_my_return_home_does_not_make_me_away(mv):
    svc = mv.svc
    rm.st_in(svc, **{"in": "serving"})
    svc.reserve_set_out("home")
    svc.rsv_update("rsv_out", since=timeutil.now_iso())
    svc.rsv_update("rsv_peer_state", at="2026-01-01T00:00:00+00:00")    # st до моего перехода домой
    assert svc.reserve_out_tick() == [] and svc.reserve_out() == "home", "старое st вернуло меня в away"


@pytest.mark.parametrize("inn", ["serving"])
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
    """Запись уже указала на резерв, а передача бота не состоялась: без возврата записи пользователи
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


def test_go_req_carries_the_ttl_of_the_record_for_the_pause_before_reconnect(mv):
    """TTL записи у основы (последняя проверка) едет в go_req: гость ждёт его,
    прежде чем звать пользователей переподключиться."""
    svc = mv.svc
    svc.rsv_update("rsv_dns", ttl=900)
    svc.reserve_move_start()
    rm.confirm_package(svc, gen=4)
    go = [a for a in svc.reserve_out_tick() if a[:2] == ("send", "go_req")]
    assert go and go[0][2]["ttl"] == 900, go


def test_a_go_without_ttl_from_an_older_primary_passes_the_check():
    assert pairproto.check(pairproto.PAIR_PROTO, "go", {"k": "ab" * 32, "gen": 4, "kind": "button"},
                           pairproto.INVITER)["gen"] == 4
