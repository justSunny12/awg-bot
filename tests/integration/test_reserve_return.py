"""Возврат клиентов с резерва на стороне основы (domain/services/reserve/outbound.py):
здоровье основы и рычаг возврата, `back_req`, реплика и слияние (пиры до
сверки, блокировки, расход через резерв), перезапуск, запись на себя, `back`,
`wiped` и новый `K`.

Цена ошибки: возврат по первой же секунде «здорова» — клиенты прыгают туда-сюда
при каждом сбое; устройство, заведённое на резерве и не попавшее в конфиг до
первой сверки пиров, сверка сочтёт чужим и удалит — человек без VPN после
возврата; прежний `K` после возврата — резерв, которому мы только что
отдали клиентов, остаётся способным открыть следующие пакеты; `wiped`,
обработанный дважды, — второй новый `K` при уже отправленном пакете;
слияние, упавшее молча, — клиенты возвращены без данных резерва.
"""
from __future__ import annotations

import os

import pytest

from awgbot.core import config, settings
from awgbot.domain.services.reserve import outbound
from awgbot.domain.services.types import ServiceError
from awgbot.infra import awg
from awgbot.util import timeutil
from awgbot.util.rsvpkg import seal

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import OWN_IP, notices, sent

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv


def _healthy_for(w, seconds: float) -> list:
    """Тик на каждые 3 с, пока не пройдёт `seconds`; возвращает все действия за это время."""
    acts: list = []
    end = w.clock.t + seconds
    while w.clock.t < end:
        acts += w.svc.reserve_out_tick()
        w.clock.advance(3)
    return acts


@pytest.fixture()
def away(mv):
    """Клиенты у резерва, шлюзов нет (иначе здоровье зависит от их связи), рычаг «сразу»."""
    mv.rsv.gw = False
    settings.set_value("app.reserve.return", "immediate")
    rm.go_away(mv.svc)
    # резерв несёт клиентов (st с in=serving): иначе «резерв на связи без моего гостя» —
    # возврат без реплики сразу, а не просьба реплики
    rm.st_in(mv.svc, **{"in": "serving"})
    return mv


# ── здоровье и рычаг ────────────────────────────────────────────────────────

def test_the_primary_is_healthy_when_its_awg_the_pair_link_and_the_gateways_are_alive(away):
    svc = away.svc
    assert svc.reserve_healthy() is True
    svc.db.set_state("last_server_ok", "0")
    assert svc.reserve_healthy() is False, "свой AWG не отвечает — возвращать некого"
    svc.db.set_state("last_server_ok", "1")
    svc._lv.state = "down"
    assert svc.reserve_healthy() is False, "линк пары молчит — слияние не пройдёт"
    svc._lv.state = "ok"
    svc.rsv_update("rsv_peer_state", session=False)
    assert svc.reserve_healthy() is False, "сессии с резервом нет"


def test_a_gateway_without_a_session_makes_the_primary_unhealthy(away, monkeypatch):
    """РФ-доступ идёт через шлюзы резерва, пока свои не на связи: вернуться раньше — клиентов
    лишить РФ-части."""
    import types
    svc = away.svc
    away.rsv.gw = True
    monkeypatch.setattr(svc.db, "gateways", lambda: [types.SimpleNamespace(id=1)])
    live = {"session": {}}
    monkeypatch.setattr(svc, "gwlink_session", lambda gid: live["session"], raising=False)
    assert svc.reserve_healthy() is False
    live["session"] = {"agent": "3.4.0"}
    assert svc.reserve_healthy() is True


def test_the_st_tells_the_reserve_about_health_and_the_button_only_while_away(away):
    svc = away.svc
    body = svc.reserve_st_body()
    assert body["healthy"] is True and body["can_return"] is False
    svc.rsv_put("rsv_out", None)
    assert svc.reserve_st_body()["healthy"] is False, "дома «здоровье» не сообщается — признак про возврат"


def test_the_return_starts_by_itself_only_after_five_healthy_minutes(away):
    svc = away.svc
    assert _healthy_for(away, outbound.HEALTHY_S - 30) == [] and svc.reserve_out() == "away"
    acts = _healthy_for(away, 60)
    assert ("send", "back_req", {}) in acts
    assert svc.reserve_out() == "returning" and svc.rsv_get("rsv_out")["step"] == "back_req"
    assert svc.rsv_get("rsv_out")["return_why"] == "auto"


def test_a_hiccup_of_the_primary_resets_the_five_minutes(away):
    svc = away.svc
    _healthy_for(away, outbound.HEALTHY_S - 20)
    svc.db.set_state("last_server_ok", "0")
    svc.reserve_out_tick()
    svc.db.set_state("last_server_ok", "1")
    away.clock.advance(3)
    assert _healthy_for(away, outbound.HEALTHY_S - 30) == [] and svc.reserve_out() == "away", \
        "накопленные 5 минут не сброшены сбоем — возврат на полусбойной основе"
    _healthy_for(away, 60)
    assert svc.reserve_out() == "returning"


def test_the_minimum_interval_between_switches_holds_the_return(away):
    """Только что переехали (30 мин по умолчанию между переключениями): основа здорова, но ждёт."""
    svc = away.svc
    svc.rsv_update("rsv_out", last_switch=timeutil.to_iso(timeutil.shift(timeutil.now(), -10 * 60)))
    _healthy_for(away, outbound.HEALTHY_S + 60)
    assert svc.reserve_out() == "away", "возврат раньше min_interval_minutes — переключения дребезжат"
    svc.rsv_update("rsv_out", last_switch=timeutil.to_iso(timeutil.shift(timeutil.now(), -31 * 60)))
    _healthy_for(away, 6)
    assert svc.reserve_out() == "returning"


def test_with_the_button_lever_the_primary_only_raises_the_flag_once(away):
    svc = away.svc
    settings.set_value("app.reserve.return", "button")
    live = [a for a in _healthy_for(away, 400) if a == ("live",)]
    assert svc.reserve_out() == "away", "рычаг «по кнопке»: сам не возвращается"
    assert svc.reserve_can_return() is True and len(live) == 1, "флаг поднят один раз, экран перерисован один раз"
    assert svc.reserve_st_body()["can_return"] is True, "резерв не узнает, что кнопку пора показать"
    svc.db.set_state("last_server_ok", "0")
    svc.reserve_out_tick()
    assert svc.reserve_can_return() is False and svc.reserve_st_body()["can_return"] is False, \
        "основа опять нездорова — кнопка у резерва должна исчезнуть"


def test_the_return_cannot_start_when_the_clients_are_not_away_or_the_reserve_is_gone(away):
    svc = away.svc
    svc.rsv_update("rsv_peer_state", session=False)
    with pytest.raises(ServiceError, match="не на связи"):
        svc.reserve_return_start("button")
    svc.rsv_update("rsv_peer_state", session=True)
    svc.reserve_set_out("home")
    with pytest.raises(ServiceError, match="не у резерва"):
        svc.reserve_return_start("button")


def test_evict_from_the_reserve_starts_the_return_and_is_harmless_otherwise(away):
    svc = away.svc
    acts = svc.reserve_evict_in()
    assert svc.reserve_out() == "returning" and ("send", "back_req", {}) in acts
    assert svc.rsv_get("rsv_out")["return_why"] == "evict"
    assert svc.reserve_evict_in() == [] or svc.reserve_out() == "returning", "повторный evict не ломает возврат"
    svc.reserve_set_out("home")
    assert svc.reserve_evict_in() == [], "evict при клиентах дома — просьба без предмета, без исключения"


def test_the_back_req_is_repeated_once_per_period_while_no_replica_arrives(away):
    svc = away.svc
    svc.reserve_return_start("button")
    away.clock.advance(outbound.RELEASED_WAIT_S - 5)
    assert svc.reserve_out_tick() == []
    away.clock.advance(10)
    assert svc.reserve_out_tick() == [("send", "back_req", {})]
    assert svc.reserve_out_tick() == [], "просьба повторена, срок начался заново"


# ── слияние ─────────────────────────────────────────────────────────────────

class _Peers:
    """Журнал пиров основы: что попало в конфиг и в каком порядке относительно сверки блокировок."""

    def __init__(self, svc, fake_awg, monkeypatch):
        self.events: list[tuple] = []
        self.fake = fake_awg
        real_add, real_rm = awg.add_peer, awg.remove_peer

        def add(pub, psk, ip, iface=None):
            self.events.append(("add", pub))
            real_add(pub, psk, ip, iface)

        def rm_(pub, iface=None):
            self.events.append(("remove", pub))
            real_rm(pub, iface)
        monkeypatch.setattr(awg, "add_peer", add)
        monkeypatch.setattr(awg, "remove_peer", rm_)
        monkeypatch.setattr(awg, "show_dump", lambda iface=None: [{"public_key": p} for p in fake_awg.peers], raising=False)
        monkeypatch.setattr(svc, "reconcile_blocks", lambda prune=False: self.events.append(("reconcile",)) or 0)

    def index(self, ev):
        return self.events.index(ev)


@pytest.fixture()
def merge_world(away, fake_awg, make_active_client, monkeypatch):
    """Основа: профиль «Аня» с устройствами `keep` и `gone`, снимок `base-7` снят, клиенты у
    резерва, возврат начат. `peers` — журнал пиров."""
    svc = away.svc
    anna = make_active_client(name="Аня", tg_id=7001)
    keep = svc.add_device(anna.id, "Телефон")
    gone = svc.add_device(anna.id, "Планшет")
    rm.put_base(svc, gen=7)
    rm.go_away(svc, gen=7)
    svc.reserve_return_start("button")
    away.peers = _Peers(svc, fake_awg, monkeypatch)
    away.anna, away.keep, away.gone = anna, keep, gone
    return away


def _reserve_edit(w, new_pub="DUMMY-pub-new"):
    """Что люди сделали на резерве: удалили «Планшет», завели профиль «Вера» с устройством."""
    gone_id = w.gone.device_id

    def edit(db):
        db.delete_device(gone_id)
        cid = db.create_client("Вера", 3, "2026-01-01T00:00:00+03:00", "2099-01-01T00:00:00+03:00", "INV-vera")
        db.activate_client(cid, 7002)
        db.create_device(cid, "Ноутбук", new_pub, "DUMMY-psk", "10.8.1.77")
    return edit


def test_a_device_created_on_the_reserve_reaches_the_config_before_the_peers_are_reconciled(merge_world):
    """Сверка блокировок и пиров после слияния не должна видеть устройство резерва «лишним»."""
    w = merge_world
    acts = w.svc.reserve_released_in(rm.snapshot_blob(w.svc, _reserve_edit(w)))
    assert acts == [("restart",)] and w.svc.rsv_get("rsv_out")["step"] == "restart"
    ev = w.peers.events
    assert ("add", "DUMMY-pub-new") in ev, ev
    assert w.peers.index(("add", "DUMMY-pub-new")) < w.peers.index(("reconcile",)), \
        f"пир добавлен после сверки блокировок: {ev}"
    assert "DUMMY-pub-new" in w.peers.fake.peers
    rep = w.svc.rsv_get("rsv_out")["merge_report"]
    assert rep["clients"] == 1 and rep["devices"] == 1 and rep["peers_added"] == 1


def test_a_device_removed_on_the_reserve_is_removed_from_the_config(merge_world):
    w = merge_world
    gone_pub = w.svc.db.get_device(w.gone.device_id).public_key
    assert gone_pub in w.peers.fake.peers
    w.svc.reserve_released_in(rm.snapshot_blob(w.svc, _reserve_edit(w)))
    assert gone_pub not in w.peers.fake.peers, "удалённое на резерве устройство осталось работать в конфиге основы"
    assert w.svc.db.get_device(w.gone.device_id) is None
    keep_pub = w.svc.db.get_device(w.keep.device_id).public_key
    assert keep_pub in w.peers.fake.peers, "нетронутое устройство потеряно слиянием"
    assert w.svc.rsv_get("rsv_out")["merge_report"]["peers_removed"] == 1


def test_the_merge_without_changes_on_the_reserve_touches_no_peer(merge_world):
    w = merge_world
    w.svc.reserve_released_in(rm.snapshot_blob(w.svc))
    assert [e for e in w.peers.events if e[0] in ("add", "remove")] == []
    assert w.svc.rsv_get("rsv_out")["step"] == "restart"


def test_the_temporary_replica_file_is_removed_after_the_merge(merge_world):
    w = merge_world
    w.svc.reserve_released_in(rm.snapshot_blob(w.svc))
    out = os.path.join(str(config.DATA_DIR), "reserve", "out")
    assert not [n for n in os.listdir(out) if n.startswith("rep-")], "расшифрованная реплика резерва осталась на диске"


def test_a_replica_under_another_key_is_refused_and_the_clients_stay_at_the_reserve(merge_world):
    w = merge_world
    acts = w.svc.reserve_released_in(rm.snapshot_blob(w.svc, k=b"\x02" * 32))
    out = w.svc.rsv_get("rsv_out")
    assert out["state"] == "away" and out["merge_error"], "слияние сорвалось, а возврат продолжается"
    text = " ".join(n.text for n in notices(acts))
    assert "Возврат не состоялся" in text and "остаются на" in text
    assert [n for n in notices(acts)][0].force_sound is True
    assert not [e for e in w.peers.events if e[0] in ("add", "remove")]


def test_a_missing_base_snapshot_merges_without_deletions_over_a_copy_of_the_live_base(merge_world):
    """Снимка нет (основа восстановлена из копии — снимки в неё не входят):
    возврат не встаёт, слияние идёт по копии живой базы без удалений, строка
    об этом — в отчёте, копия после слияния убрана."""
    w = merge_world
    out_dir = os.path.join(str(config.DATA_DIR), "reserve", "out")
    os.unlink(os.path.join(out_dir, "base-7.db"))
    acts = w.svc.reserve_released_in(rm.snapshot_blob(w.svc))
    assert acts == [("restart",)], f"без снимка возврат встал: {w.svc.rsv_get('rsv_out')}"
    assert w.svc.reserve_out() == "returning" and not w.svc.rsv_get("rsv_out").get("merge_error")
    lost = w.svc.rsv_get("rsv_out").get("merge_report", {}).get("lost") or []
    assert any("снимка для слияния не было" in x for x in lost), lost
    assert not [n for n in os.listdir(out_dir) if "fallback" in n or n.startswith("rep-")], os.listdir(out_dir)
    assert not [e for e in w.peers.events if e[0] == "remove"], "без снимка пиры сняты — удаление выведено из копии"


def test_a_replica_outside_a_return_is_ignored(away):
    svc = away.svc
    assert svc.reserve_released_in(b"junk") == [] and svc.reserve_out() == "away"
    svc.reserve_return_start("button")
    svc._out_step("relay")
    assert svc.reserve_released_in(b"junk") == [], "реплика на позднем шаге не должна запускать слияние заново"


# ── после слияния: перезапуск, Telegram, запись, хвост ──────────────────────

def _at_step(w, step: str, **fields):
    w.svc.reserve_set_out("returning", kind="button", gen=7, return_started_at=timeutil.now_iso(), **fields)
    w.svc._out_step(step, 300)


def test_the_restart_step_repeats_until_the_bot_has_restarted_and_then_takes_telegram(away):
    svc = away.svc
    _at_step(away, "restart")
    assert svc.reserve_out_tick() == [("restart",)]
    assert svc.reserve_return_after_restart() is True
    assert svc.reserve_out_tick() == [("take_tg",)]
    assert svc.reserve_return_after_restart() is True, "повторный старт на шаге take_tg тоже добирает Telegram"


def test_after_the_restart_hook_nothing_happens_outside_a_return(away):
    assert away.svc.reserve_return_after_restart() is False


def test_the_telegram_is_mine_again_only_after_the_webhook_is_gone(away):
    svc = away.svc
    _at_step(away, "take_tg")
    acts = svc.reserve_tg_taken_mark()
    assert svc.reserve_poll() == "mine" and ("send", "taken", {}) in acts
    out = svc.rsv_get("rsv_out")
    assert out["step"] == "record_back" and out["record_set_at"] == ""
    svc.reserve_set_out("away")
    assert svc.reserve_tg_taken_mark() == [], "вне возврата отметка ничего не двигает"


def test_the_record_goes_back_by_the_manual_fact_and_then_the_reserve_gets_back(away):
    svc = away.svc
    away.dns.auth = [rm.PEER_IP]
    _at_step(away, "record_back", record_set_at="", record_probe=0)
    away.clock.advance(30)
    acts = svc.reserve_out_tick()
    assert "back" not in sent(acts) and away.dns.sets == [], "запись ещё не наша, ручной режим"
    away.dns.auth = [OWN_IP]
    away.clock.advance(30)
    acts = svc.reserve_out_tick()
    assert sent(acts) == ["back"], acts
    out = svc.rsv_get("rsv_out")
    assert out["step"] == "relay" and out["record_fact_at"]
    away.clock.advance(5)
    assert svc.reserve_out_tick() == [], "back повторён раньше минуты — резерв ещё мог не ответить relay"
    away.clock.advance(60)
    assert sent(svc.reserve_out_tick()) == ["back"], "relay не пришёл, а back не повторён — возврат ждёт срок впустую"
    svc.reserve_relay_in("2026-10-28T00:00:00+05:00")
    away.clock.advance(120)
    assert svc.reserve_out_tick() == [], "relay пришёл, а back всё повторяется"


def test_the_automatic_return_changes_the_record_to_my_address_exactly_once(away):
    svc = away.svc
    rm.automatic(svc)
    away.dns.auth = [rm.PEER_IP]
    _at_step(away, "record_back", record_set_at="", record_probe=0)
    for _ in range(3):
        away.clock.advance(30)
        svc.reserve_out_tick()
    assert away.dns.sets == [OWN_IP]


def test_a_failing_provider_on_the_return_asks_the_human_once_and_keeps_waiting(away):
    svc = away.svc
    rm.automatic(svc)
    away.dns.set_error = "HTTP 403"
    away.dns.auth = [rm.PEER_IP]
    _at_step(away, "record_back", record_set_at="", record_probe=0)
    away.clock.advance(30)
    acts = svc.reserve_out_tick()
    texts = [n.text for n in notices(acts)]
    assert len(texts) == 1 and "403" in texts[0] and OWN_IP in texts[0] and rc.HOST_A in texts[0]
    assert notices(acts)[0].force_sound is True
    assert svc.reserve_out() == "returning", "возврат не бросаем: человек сменит запись руками"
    away.clock.advance(30)
    assert not notices(svc.reserve_out_tick()), "то же уведомление на каждом тике"
    away.dns.auth = [OWN_IP]
    away.clock.advance(30)
    assert sent(svc.reserve_out_tick()) == ["back"]


def test_the_relay_term_from_the_reserve_is_stored(away):
    svc = away.svc
    _at_step(away, "relay")
    assert svc.reserve_relay_in("2026-10-28T00:00:00+05:00") == [("live",)]
    assert svc.rsv_get("rsv_out")["relay_until"] == "2026-10-28T00:00:00+05:00"
    svc.reserve_set_out("home")
    assert svc.reserve_relay_in("x") == []


# ── wiped: новый K ──────────────────────────────────────────────────────────

def _package_under(svc):
    """Пакет, собранный сейчас, — как его увидит резерв."""
    return bytes.fromhex(svc.reserve_pkg_build(force=True)["blob"])


@pytest.mark.parametrize("kind", ["button", "dead"])
def test_wiped_ends_the_return_with_a_new_k_and_the_old_package_is_unreadable_for_it(away, kind):
    svc = away.svc
    old_k = svc.reserve_k()
    old_pkg = _package_under(svc)
    _at_step(away, "relay", merge_report={"clients": 1, "devices": 2})
    svc.rsv_update("rsv_out", kind=kind, record_fact_at=timeutil.now_iso())
    svc.rsv_put("rsv_shares", {"x": "DUMMY"})
    acts = svc.reserve_wiped_in()
    assert svc.reserve_out() == "home"
    new_k = svc.reserve_k()
    assert new_k != old_k and len(new_k) == 32
    with pytest.raises(seal.PackageError):
        seal.open_parts(old_pkg, new_k)
    assert seal.open_parts(old_pkg, old_k), "прежний пакет читается своим K (контроль теста)"
    away.clock.advance(11)
    built = svc.reserve_pkg_tick()
    assert built is not None, "после возврата новый пакет не запрошен — у резерва остаётся только прежний"
    blob = bytes.fromhex(built["blob"])
    assert seal.open_parts(blob, new_k)
    with pytest.raises(seal.PackageError):
        seal.open_parts(blob, old_k)
    assert svc.rsv_get("rsv_shares") == {}, "доли прежнего пакета не сброшены"
    texts = [n.text for n in notices(acts)]
    assert any("снова здесь" in t and "1 новых профиля" in t for t in texts)
    token_notice = [t for t in texts if "Токен API DNS" in t]
    assert bool(token_notice) is (kind == "dead"), \
        "просьба сменить токен нужна только после аварийного переезда (токен был у резерва)"


def test_a_second_wiped_does_not_change_the_key_again(away):
    svc = away.svc
    _at_step(away, "relay")
    svc.reserve_wiped_in()
    k = svc.reserve_k()
    assert svc.reserve_wiped_in() == [] and svc.reserve_k() == k, \
        "повторный wiped (повтор сообщения по новой сессии) сменил K под уже отправленным пакетом"


def test_the_return_ends_by_itself_when_wiped_never_comes(away):
    svc = away.svc
    old_k = svc.reserve_k()
    svc.reserve_set_out("returning", kind="button", gen=7)
    svc._out_step("relay", outbound.RELAY_WAIT_S)
    svc.reserve_relay_in("2026-10-28T00:00:00+05:00")      # relay пришёл, wiped — нет
    assert svc.reserve_out_tick() == []
    away.clock.advance(outbound.RELAY_WAIT_S + 1)
    acts = svc.reserve_out_tick()
    assert svc.reserve_out() == "home" and svc.reserve_k() != old_k
    assert any("снова здесь" in n.text for n in notices(acts))


def test_the_return_notice_shows_the_traffic_through_the_reserve_and_no_final_period(away):
    svc = away.svc
    svc.reserve_traffic_out_add(3 * 1024 ** 3, 1024 ** 3)
    _at_step(away, "relay", merge_report={"clients": 0, "devices": 0})
    texts = [n.text for n in notices(svc.reserve_wiped_in())]
    assert "через резерв" in texts[0] and "без новых профилей и устройств" in texts[0]
    assert not texts[0].rstrip().endswith(".")


def test_lost_devices_of_the_report_are_listed_in_the_notice(away):
    svc = away.svc
    line = svc._lost_line({"name": "Планшет", "owner": "Аня", "taken_by": "Ноутбук", "taken_owner": "Вера"})
    _at_step(away, "relay", merge_report={"lost": [line]})
    text = notices(svc.reserve_wiped_in())[0].text
    assert "Не встало устройство" in text and "Планшет" in text and "Выдай" in text


# ── новое поколение ключа — только после возврата по долям или коду ─────────

@pytest.mark.parametrize("kind, go_sent, regen", [
    ("button", True, False), ("rf", True, False), ("nogw", True, False),   # K отдан мной (`go`) — прямая цела
    ("button", False, True), ("dead", False, True), ("code", False, True),  # своего go не было — по долям/коду
    ("button", None, False), ("rf", None, False), ("nogw", None, False),    # отметки нет (прежнее состояние):
    ("dead", None, True), ("code", None, True),                             # по виду переезда
])
def test_the_generation_changes_after_a_return_only_when_the_package_was_opened_by_shares_or_code(away, kind, go_sent,
                                                                                                   regen):
    """Доли и код раскрывают вторую точку прямой (и конверт DNS): после
    такого возврата — новое поколение M, код админу и просьба сменить токен
    DNS; после переезда ключом основы — ничего из этого (лишний код админу и
    лишний перевыпуск долей шлюзов). Чем открыт пакет, основа решает по своей
    отметке отправки K (go_sent), а без неё — по виду переезда."""
    svc = away.svc
    m_gen = int(svc.reserve_keys()["m_gen"])
    _at_step(away, "relay")
    fields = dict(kind=kind, by="", record_fact_at=timeutil.now_iso())
    if go_sent is not None:
        fields["go_sent"] = go_sent
    svc.rsv_update("rsv_out", **fields)
    texts = [n.text for n in notices(svc.reserve_wiped_in())]
    tag = f"{kind}/go_sent={go_sent}"
    assert (int(svc.reserve_keys()["m_gen"]) == m_gen + 1) is regen, f"{tag}: поколение M"
    assert bool(svc.rsv_get("rsv_keys").get("code_notify_why")) is regen, f"{tag}: код админу"
    assert bool([t for t in texts if "Токен API DNS" in t]) is regen, f"{tag}: просьба сменить токен"


@pytest.mark.parametrize("in_by", ["k", "share", ""])
def test_the_word_of_the_reserve_does_not_spare_the_generation_when_my_k_was_not_sent(away, in_by):
    """Резерв, собравший M по долям, мог бы сказать «открыл ключом» (in_by=k)
    — и при доверии к его слову поколение осталось бы прежним, а он сохранил
    бы способность открывать следующие пакеты. Решает только своя отметка."""
    svc = away.svc
    m_gen = int(svc.reserve_keys()["m_gen"])
    _at_step(away, "relay")
    svc.rsv_update("rsv_out", kind="button", by="k", go_sent=False, record_fact_at=timeutil.now_iso())
    svc.rsv_update("rsv_peer_state", in_by=in_by)
    svc.reserve_wiped_in()
    assert int(svc.reserve_keys()["m_gen"]) == m_gen + 1, f"in_by={in_by!r}: поколение не сменилось без моего go"


def test_my_own_go_keeps_the_generation_even_if_the_reserve_says_shares(away):
    """Обратная сторона: K отдан мной — прямая цела; слово резерва «по долям»
    не заставляет перевыпускать доли шлюзов и слать админу новый код."""
    svc = away.svc
    m_gen = int(svc.reserve_keys()["m_gen"])
    _at_step(away, "relay")
    svc.rsv_update("rsv_out", kind="button", by="share", go_sent=True, record_fact_at=timeutil.now_iso())
    svc.rsv_update("rsv_peer_state", in_by="share")
    svc.reserve_wiped_in()
    assert int(svc.reserve_keys()["m_gen"]) == m_gen, "поколение сменилось после переезда моим K"
    assert svc.rsv_get("rsv_out").get("go_sent") is False, "отметка go_sent пережила возврат — следующий переезд её унаследует"
