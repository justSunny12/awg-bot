"""С9 у основы: резерв лёг, пока нёс моих пользователей, — рычаг «вернуть пользователей
без резерва» (outbound: reserve_force_check / reserve_force_home /
reserve_stuck_home с await_merge / _tick_await_merge / _return_done) и
финальная реплика передачи как снимок слияния (reserve_release_build).

- рычаг возможен только при away и резерве не на связи ≥ FORCE_OFFLINE_S;
- запись — на свой адрес через API, если она автоматическая; отказ API —
  громкое Т-РУ11, но рычаг идёт (Telegram важнее); вручную — API не трогается;
- `take_home` → пользователи на основе с отметкой await_merge, вид переезда сохранён, K прежний
  (реплика резерва под ним), одно уведомление «пользователи снова здесь»;
- резерв вышел на связь и несёт гостя — обычный возврат с back_req
  (return_why = force); вышел пустым — отметка снята, новый K, пакет,
  уведомление «не вольются»; конец возврата снимает отметку;
- deleteWebhook рычага не прошёл — повтор раз в минуту, ~5 минут отказов — одно
  уведомление со звуком.

Окружение — `mv` из _reserve_move (пара, запись DNS и часы автоматов — модели).

Цена ошибки: рычаг при живом резерве — два бота на один токен и пользователи по
обе стороны; новый K на рычаге — реплику резерва с изменениями за время
переезда не открыть, они теряются; отметка, не снятая после возврата, —
следующий выход резерва на связь затевает лишний возврат; без снимка
финальной реплики слияние считает «чужим» всё, что записано здесь между
пакетом и передачей, — удвоения и откаты профилей; take_home без повтора — бот
нем, пока не сработает путь зависшего вебхука (полчаса и больше), и админ не знает.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from awgbot.core import config
from awgbot.domain.services.reserve import outbound, outbound_return
from awgbot.domain.services.types import ServiceError
from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import OWN_IP, notices, sent

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv


def _ago(minutes: float) -> str:
    return timeutil.to_iso(timeutil.shift(timeutil.now(), -minutes * 60))


def _pkg_due(svc) -> bool:
    return svc.__dict__.get("_rsv_pkg_due_at") is not None


@pytest.fixture()
def gone(mv):
    """Мои пользователи у резерва (вид dead), резерв не на связи 11 минут, записью
    управляет бот; основа нездорова — возврат по здоровью не вмешивается."""
    svc = mv.svc
    rm.go_away(svc, kind="dead")
    rm.automatic(svc)
    svc.db.set_state("last_server_ok", "0")
    svc.rsv_update("rsv_peer_state", session=False, down_since=_ago(11))
    return mv


# ── когда рычаг возможен ────────────────────────────────────────────────────

def test_the_lever_is_possible_when_the_reserve_holding_my_clients_is_gone_ten_minutes(gone):
    assert gone.svc.reserve_force_check() == ""


@pytest.mark.parametrize("prep, word", [
    (lambda s: s.reserve_set_out("home"), "не у резерва"),
    (lambda s: (s.rsv_update("rsv_peer_state", session=True), setattr(s._lv, "state", "ok")), "на связи"),   # сеанс и живость
    (lambda s: s.rsv_update("rsv_peer_state", down_since=_ago(9)), "меньше 10 мин"),
    (lambda s: s.rsv_update("rsv_peer_state", down_since=""), "меньше 10 мин"),
    (lambda s: s.rsv_update("rsv_peer_state", down_since="вчера"), "не известен"),
])
def test_the_lever_is_refused_with_the_reason(gone, prep, word):
    prep(gone.svc)
    why = gone.svc.reserve_force_check()
    assert why and word in why, why
    with pytest.raises(ServiceError):
        gone.svc.reserve_force_home()
    assert not gone.svc.rsv_get("rsv_out").get("await_merge"), "отказанный рычаг поставил отметку"


def test_the_threshold_is_ten_minutes():
    assert outbound.FORCE_OFFLINE_S == 600


# ── рычаг ───────────────────────────────────────────────────────────────────

def test_the_lever_switches_the_automatic_record_home_and_asks_to_take_telegram(gone):
    k = gone.svc.reserve_k()
    acts = gone.svc.reserve_force_home()
    assert gone.dns.sets == [OWN_IP], f"запись не возвращена на свой адрес: {gone.dns.sets}"
    assert acts == [("take_home",)], acts
    out = gone.svc.rsv_get("rsv_out")
    assert out.get("await_merge") is True and out.get("force_at"), out
    assert gone.svc.reserve_k() == k, "K сменён на рычаге — реплику резерва будет нечем открыть"


def test_the_lever_with_a_manual_record_does_not_touch_the_api(gone):
    gone.svc.reserve_dns_set_provider("manual")
    acts = gone.svc.reserve_force_home()
    assert gone.dns.sets == [] and acts == [("take_home",)], acts


def test_an_api_refusal_is_told_loudly_and_the_lever_still_goes(gone):
    gone.dns.set_error = "403 токен отозван"
    acts = gone.svc.reserve_force_home()
    assert ("take_home",) in acts, "отказ API остановил возврат Telegram"
    told = notices(acts)
    assert len(told) == 1 and told[0].force_sound and "403 токен отозван" in told[0].text and OWN_IP in told[0].text, \
        [n.text for n in told]
    assert not told[0].text.rstrip().endswith(".")
    assert gone.svc.rsv_get("rsv_out").get("await_merge") is True


def test_the_dialog_data_names_the_record_and_the_mode(gone):
    d = gone.svc.reserve_force_dialog_data()
    assert (d["name"], d["record"], d["own"], d["manual"]) == ("Beta", rc.HOST_A, OWN_IP, False), d
    assert d["since"] == gone.svc.reserve_peer_state()["down_since"]


# ── пользователи здесь, реплика ждётся ───────────────────────────────────────────

def _forced_home(w) -> list:
    w.svc.reserve_force_home()
    w.svc.__dict__.pop("_rsv_pkg_due_at", None)
    return w.svc.reserve_stuck_home()


def test_after_take_home_the_clients_are_home_waiting_for_the_replica_with_one_notice(gone):
    k = gone.svc.reserve_k()
    acts = _forced_home(gone)
    out = gone.svc.rsv_get("rsv_out")
    assert gone.svc.reserve_out() == "home" and gone.svc.reserve_poll() == "mine"
    assert out.get("await_merge") is True and out.get("kind") == "dead", f"вид переезда потерян: {out}"
    assert gone.svc.reserve_k() == k and not _pkg_due(gone.svc), "K сменён до слияния"
    texts = [n.text for n in notices(acts)]
    assert len(texts) == 1 and "Пользователи и бот снова здесь" in texts[0] and "«Beta»" in texts[0], texts
    assert "вебхук снят" not in texts[0], "сказано про зависший вебхук вместо рычага"
    assert not texts[0].rstrip().endswith(".")
    assert ("live",) in acts


def test_a_reserve_still_offline_keeps_the_mark_and_does_nothing(gone):
    _forced_home(gone)
    gone.clock.advance(600)
    assert gone.svc.reserve_out_tick() == [] and gone.svc.rsv_get("rsv_out").get("await_merge") is True


def test_the_reserve_back_with_my_guest_starts_the_regular_return(gone):
    _forced_home(gone)
    rm.st_in(gone.svc, **{"in": "serving"})
    gone.svc._lv.state = "ok"                                   # живость — словом канала, не одним st
    acts = gone.svc.reserve_out_tick()
    out = gone.svc.rsv_get("rsv_out")
    assert gone.svc.reserve_out() == "returning" and out.get("step") == "back_req", out
    assert out.get("return_why") == "force" and out.get("kind") == "dead" and not out.get("await_merge"), out
    assert sent(acts) == ["back_req"] and ("live",) in acts, acts
    assert gone.svc.reserve_out_tick() == [], "back_req повторился без срока"


@pytest.mark.parametrize("inn", ["taking", "releasing"])
def test_a_reserve_that_is_opening_or_releasing_is_merged_too(gone, inn):
    _forced_home(gone)
    rm.st_in(gone.svc, **{"in": inn})
    gone.svc._lv.state = "ok"                                   # живость — словом канала, не одним st
    gone.svc.reserve_out_tick()
    assert gone.svc.reserve_out() == "returning"


def test_the_reserve_back_without_my_guest_drops_the_mark_with_a_new_k_and_one_notice(gone):
    _forced_home(gone)
    k = gone.svc.reserve_k()
    rm.st_in(gone.svc, **{"in": "idle"})
    gone.svc._lv.state = "ok"                                   # живость — словом канала, не одним st
    acts = gone.svc.reserve_out_tick()
    assert gone.svc.reserve_out() == "home" and not gone.svc.rsv_get("rsv_out").get("await_merge")
    assert gone.svc.reserve_k() != k and _pkg_due(gone.svc), "реплики не будет, а K прежний — резерв его знает"
    texts = [n.text for n in notices(acts)]
    # переезд был без моего K (вид dead, `go` не отдавал): кроме «не вольются» — просьба сменить токен DNS
    lost = [t for t in texts if "будут утеряны" in t]
    assert len(lost) == 1 and "«Beta»" in lost[0], texts
    assert not lost[0].rstrip().endswith(".")
    assert len(texts) == 2 and any("Токен API DNS" in t for t in texts), texts
    assert gone.svc.reserve_out_tick() == [], "уведомление «не вольются» повторилось"


def test_a_stale_view_of_the_reserve_does_not_leave_the_return_hanging(gone):
    """Сессия пары открылась раньше первого `st` резерва: в памяти — его
    прежнее `in = serving` (до того, как он лёг). Старый взгляд возврат не
    начинает: живым словом резерв ещё ничего не сказал (`st` устаревший, живость
    `down`) — основа ждёт дома. Резерв перезагрузился и пуст — его первый `st`
    `in = idle` снимает рычаг без слияния."""
    _forced_home(gone)
    gone.svc.rsv_update("rsv_peer_state", inn="serving")         # последний st до падения
    gone.svc.reserve_session_opened({})
    gone.svc.reserve_out_tick()
    out = gone.svc.rsv_get("rsv_out")
    assert gone.svc.reserve_out() == "home" and out.get("await_merge"), f"старый st начал возврат к тому, кто не ответит: {out}"
    k = gone.svc.reserve_k()
    rm.st_in(gone.svc, **{"in": "idle"})                         # первый st после перезагрузки
    gone.svc._lv.state = "ok"                                   # живость — словом канала, не одним st
    acts = []
    for _ in range(3):
        gone.clock.advance(outbound.RELEASED_WAIT_S + 1)
        acts += gone.svc.reserve_out_tick()
    out = gone.svc.rsv_get("rsv_out")
    assert gone.svc.reserve_out() == "home", f"резерв пуст и сказал idle, а основа ждёт его реплику без конца: {out}"
    assert not out.get("await_merge") and gone.svc.reserve_k() != k and _pkg_due(gone.svc), \
        f"возврат без слияния не снял рычаг или оставил K, который знает резерв: {out}"
    texts = [n.text for n in notices(acts)]
    assert len([t for t in texts if "будут утеряны" in t]) == 1 and len(texts) == 2, texts   # и Т-РУ17 (токен DNS)


def test_the_end_of_the_return_drops_the_mark(gone):
    gone.svc.reserve_set_out("returning", kind="dead", await_merge=True, return_why="force")
    gone.svc._out_step("relay", 1)
    gone.svc._return_done([])
    assert gone.svc.reserve_out() == "home" and gone.svc.rsv_get("rsv_out").get("await_merge") is False


def test_a_stuck_webhook_without_the_lever_still_renews_k(gone):
    """Без рычага (обычный зависший вебхук) — прежнее поведение: новый K."""
    k = gone.svc.reserve_k()
    acts = gone.svc.reserve_stuck_home()
    assert gone.svc.reserve_k() != k and not gone.svc.rsv_get("rsv_out").get("await_merge")
    assert "вебхук снят" in " ".join(n.text for n in notices(acts))


# ── 5в: снимок слияния — финальная реплика ──────────────────────────────────

def _base(gen: int) -> str:
    return os.path.join(str(config.DATA_DIR), "reserve", "out", f"base-{gen}.db")


def test_the_final_replica_of_the_handover_becomes_the_merge_base(mv, make_active_client):
    """Пакет снят раньше, после него здесь завели профиль: база слияния — та,
    с которой стартует гость, — обязана его содержать."""
    svc = mv.svc
    rm.put_base(svc, gen=9)
    old = open(_base(9), "rb").read()
    make_active_client(name="Поздний", tg_id=7333)
    rm.to_step(svc, "release", gen=9)
    built = svc.reserve_release_build()
    assert built and built["gen"] == 9
    assert open(_base(9), "rb").read() != old, "снимок слияния остался от пакета, а не от финальной реплики"
    con = sqlite3.connect(_base(9))
    try:
        names = [r[0] for r in con.execute("SELECT name FROM clients")]
    finally:
        con.close()
    assert "Поздний" in names, f"в снимке слияния нет профиля, заведённого после пакета: {names}"
    assert oct(os.stat(_base(9)).st_mode & 0o777) == "0o600", "снимок с базой пользователей шире 0600"


def test_a_merge_base_that_cannot_be_written_does_not_stop_the_handover(mv, monkeypatch):
    svc = mv.svc
    rm.to_step(svc, "release", gen=9)

    def boom(gen, data):
        raise OSError("нет места")
    monkeypatch.setattr(svc, "_pkg_store_base", boom)
    built = svc.reserve_release_build()
    assert built and svc.rsv_get("rsv_out")["step"] == "released", "передача остановлена из-за снимка"


# ── deleteWebhook рычага не прошёл ──────────────────────────────────────────

def test_a_failed_take_of_the_lever_is_repeated_every_minute_and_told_after_five(gone):
    """take_home ушёл, а deleteWebhook отказал (Telegram недоступен с основы): основа в away с
    отметкой await_merge и без поллинга — повтор раз в минуту, ~5 минут отказов — Т-РУ со звуком
    один раз; автоматического возврата по здоровью в это время нет."""
    svc = gone.svc
    assert svc.reserve_force_home() == [("take_home",)]
    assert svc.reserve_out_tick() == [], "повтор сразу за первой попыткой"
    takes, told = 0, []
    for _ in range(12):
        gone.clock.advance(outbound_return.FORCE_TAKE_RETRY_S * 1.2 + 1)
        svc.rsv_update("rsv_out", force_at=_ago((gone.clock.t - 1000) / 60))
        acts = svc.reserve_out_tick()
        takes += acts.count(("take_home",))
        told += notices(acts)
        assert svc.reserve_out() == "away", acts
    assert takes == 12, f"deleteWebhook повторён {takes} раз из 12 минут"
    assert len(told) == 1 and told[0].force_sound and "deleteWebhook" in told[0].text and "«Beta»" in told[0].text, \
        [n.text for n in told]
    assert not told[0].text.rstrip().endswith(".")


def test_the_lever_take_that_finally_passes_ends_the_repeats(gone):
    svc = gone.svc
    svc.reserve_force_home()
    gone.clock.advance(80)
    assert svc.reserve_out_tick() == [("take_home",)]
    svc.reserve_stuck_home()                                  # теперь прошёл
    gone.clock.advance(80)
    assert ("take_home",) not in svc.reserve_out_tick()
    assert svc.reserve_out() == "home" and svc.rsv_get("rsv_out").get("await_merge") is True
