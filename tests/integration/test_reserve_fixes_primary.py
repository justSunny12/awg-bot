"""Правки автоматов «мои клиенты» на стороне основы
(domain/services/reserve/outbound.py) после двух ревью: своя отметка
отправки K (go_sent) вместо слова резерва, новый K при срыве после `go`,
зависший вебхук резерва (take_home), повторы `back`/`back_req` и сроки шагов
возврата, поколение снимка из реплики, строки слияния о Telegram-аккаунте и
устройствах админа, сторож обновления tools/rsvbusy.py.

Запись DNS, часы автоматов и канал пары — подставные (tests/integration/_reserve_move).

Цена ошибки: K, отданный резерву при сорванном переезде и не сменённый, —
резерв открывает следующие пакеты без согласия основы; вебхук резерва,
оставшийся после оборванного возврата, — бот основы молчит, хотя клиенты
уже дома; снятый раньше 10 минут — короткий сбой пробы отбирает право
отвечать у резерва, который клиентов ещё несёт; слияние, не нашедшее
снимок по поколению, — клиенты застревают на резерве; смена Telegram у
профиля на резерве без строки в итоге — чужой аккаунт получает профиль
незаметно; обновление посреди переезда — код меняется под гостем.
"""
from __future__ import annotations

import pytest

from awgbot.core import config, settings
from awgbot.domain.services.reserve import outbound
from awgbot.runtime import rsvactions
from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm
from tests.integration._reserve_move import OWN_IP, PEER_IP, notices, sent
from tests.integration.test_reserve_actions import act
from tests.integration.test_reserve_return import away, merge_world

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv
__all__ = ["act", "away", "merge_world"]


def _pkg_due(svc) -> bool:
    """Свежий пакет запрошен вне очереди (отметка reserve_pkg_mark)."""
    return svc.__dict__.get("_rsv_pkg_due_at") is not None


def _ago(minutes: float) -> str:
    return timeutil.to_iso(timeutil.shift(timeutil.now(), -minutes * 60))


# ── go_sent: своя отметка отправки K ────────────────────────────────────────

def test_the_move_starts_without_the_mark_and_go_sets_it(mv):
    """`go` с K — единственное, что даёт резерву ключ основы; отметка ставится
    в тот момент, когда он уходит, и только тогда."""
    svc = mv.svc
    svc.rsv_update("rsv_out", go_sent=True)                    # след прежнего переезда
    svc.reserve_move_start()
    assert svc.rsv_get("rsv_out")["go_sent"] is False, "новый переезд унаследовал отметку прежнего"
    assert svc.reserve_out_tick() == [], "пакет ещё не подтверждён"
    assert svc.rsv_get("rsv_out")["go_sent"] is False, "отметка без отправленного go"
    rm.confirm_package(svc, gen=4)
    acts = svc.reserve_out_tick()
    assert sent(acts) == ["go"] and svc.rsv_get("rsv_out")["go_sent"] is True


def test_a_move_that_fails_after_go_gets_a_new_k_and_a_fresh_package(mv):
    """K ушёл резерву, переезд сорвался — резерв знает K и может открыть
    следующий пакет; нужен новый K и пакет под ним сразу."""
    svc = mv.svc
    svc.reserve_move_start()
    rm.confirm_package(svc, gen=4)
    svc.reserve_out_tick()
    k = svc.reserve_k()
    svc.__dict__.pop("_rsv_pkg_due_at", None)
    svc.reserve_down_in("порт занят")
    assert svc.reserve_out() == "home"
    assert svc.reserve_k() != k, "K, известный резерву, остался ключом следующих пакетов"
    assert _pkg_due(svc), "новый K без свежего пакета — у резерва пакет под прежним"


def test_a_move_that_fails_before_go_keeps_the_k(mv):
    svc = mv.svc
    svc.reserve_move_start()
    k = svc.reserve_k()
    mv.clock.advance(outbound.PKG_WAIT_S + 1)
    svc.reserve_out_tick()
    assert svc.reserve_out() == "home" and svc.reserve_k() == k, "K сменён, хотя резерву он не уходил"


# ── зависший вебхук резерва ─────────────────────────────────────────────────

@pytest.fixture()
def stuck(mv):
    """Клиенты числятся у резерва, право отвечать у его вебхука, а запись уже
    указывает на меня; резерв на связи и говорит in=idle; основа нездорова
    (чтобы возврат по здоровью не вмешивался)."""
    svc = mv.svc
    rm.go_away(svc)
    mv.dns.auth = [OWN_IP]
    svc.db.set_state("last_server_ok", "0")
    rm.st_in(svc, **{"in": "idle"})
    return mv


def _stuck_ticks(w, since_min: float) -> list:
    w.svc.rsv_update("rsv_out", stuck_since=_ago(since_min))
    w.clock.advance(121)
    return w.svc.reserve_out_tick()


def test_the_stuck_webhook_is_taken_home_after_ten_minutes_and_not_before(stuck):
    svc = stuck.svc
    first = svc.reserve_out_tick()
    assert ("take_home",) not in first and svc.rsv_get("rsv_out").get("stuck_since"), \
        "первая же проба сняла вебхук — отметка начала не поставлена"
    assert ("take_home",) not in _stuck_ticks(stuck, 9), "вебхук резерва снят раньше 10 минут"
    assert ("take_home",) in _stuck_ticks(stuck, 11), "запись на мне 10+ минут, резерв пуст — вебхук не снят"


def test_the_stuck_probe_is_not_run_more_often_than_every_two_minutes(stuck):
    svc = stuck.svc
    svc.reserve_out_tick()
    reads = stuck.dns.auth_reads
    svc.rsv_update("rsv_out", stuck_since=_ago(11))
    stuck.clock.advance(60)
    assert ("take_home",) not in svc.reserve_out_tick(), "проба раньше 2 минут"
    assert stuck.dns.auth_reads == reads, "авторитетный сервер опрашивается на каждом такте"


@pytest.mark.parametrize("prep, why", [
    (lambda w: setattr(w.dns, "auth", [PEER_IP]), "запись указывает на резерв"),
    (lambda w: rm.st_in(w.svc, **{"in": "serving"}), "резерв несёт клиентов"),
])
def test_no_take_home_while_the_reserve_really_holds_the_clients(stuck, prep, why):
    prep(stuck)
    stuck.svc.reserve_out_tick()
    assert ("take_home",) not in _stuck_ticks(stuck, 30), f"{why} — а вебхук снят"
    assert not stuck.svc.rsv_get("rsv_out").get("stuck_since"), "отметка не сброшена, хотя повода нет"


@pytest.mark.parametrize("down_min, want", [(31, True), (20, False)])
def test_without_the_pair_the_webhook_counts_as_stuck_only_after_thirty_minutes(stuck, down_min, want):
    svc = stuck.svc
    svc.rsv_update("rsv_peer_state", session=False, down_since=_ago(down_min))
    svc.reserve_out_tick()
    assert (("take_home",) in _stuck_ticks(stuck, 11)) is want, f"пары нет {down_min} мин"


def test_no_take_home_when_the_right_to_answer_is_mine(stuck):
    stuck.svc.reserve_set_poll("mine")
    stuck.svc.reserve_out_tick()
    assert ("take_home",) not in _stuck_ticks(stuck, 30)


def test_the_stuck_home_brings_the_clients_home_with_a_new_k_and_one_notice(stuck):
    svc = stuck.svc
    k = svc.reserve_k()
    svc.__dict__.pop("_rsv_pkg_due_at", None)
    acts = svc.reserve_stuck_home()
    assert svc.reserve_poll() == "mine" and svc.reserve_out() == "home"
    assert svc.reserve_k() != k and _pkg_due(svc), "резерв держал бот основы — K прежний"
    texts = [n.text for n in notices(acts)]
    assert len(texts) == 1 and "вебхук снят" in texts[0] and "Beta" in texts[0], texts
    assert not texts[0].rstrip().endswith("."), "уведомление с точкой в конце"


async def test_take_home_deletes_the_webhook_first_and_only_then_goes_home(stuck, act):
    rm.go_away(act.svc)
    act.pc.take_ok = False
    await rsvactions.run([("take_home",)])
    assert act.log == ["pc.take"] and act.svc.reserve_out() == "away", "вебхук не снят, а клиенты «дома»"
    act.pc.take_ok = True
    await rsvactions.run([("take_home",)])
    assert act.svc.reserve_out() == "home" and act.svc.reserve_poll() == "mine"


async def test_wake_poll_only_wakes_the_polling(mv, act):
    await rsvactions.run([("wake_poll",)])
    assert act.log == ["pc.wake"]


async def test_a_release_whose_batch_is_not_confirmed_is_put_off(mv, act):
    """Пачка обновлений не подтверждена — резерв увидел бы её снова; бот не
    передаётся, шаг release остаётся, поллинг будится."""
    rm.to_step(act.svc, "release")
    act.pc.release_ok = False
    await rsvactions.run([("release",)])
    assert act.log == ["pc.release", "pc.wake"], act.log
    assert not act.released and act.svc.rsv_get("rsv_out")["step"] == "release"


# ── возврат: повторы и сроки шагов ──────────────────────────────────────────

def _at_step(w, step: str, wait: int = 300, **fields):
    w.svc.reserve_set_out("returning", kind="button", gen=7, return_started_at=timeutil.now_iso(), **fields)
    w.svc._out_step(step, wait)


def test_a_record_that_does_not_come_back_in_time_still_sends_back_and_asks_the_human(away):
    """Запись не вернулась за RECORD_WAIT_S (API или факт): клиентов держит
    пересылка — `back` всё равно, админу Т-РУ11 со звуком, шаг relay."""
    svc = away.svc
    away.dns.auth = [PEER_IP]
    _at_step(away, "record_back", outbound.RECORD_WAIT_S, record_set_at="", record_probe=0)
    away.clock.advance(outbound.RECORD_WAIT_S - 5)
    assert "back" not in sent(svc.reserve_out_tick())
    away.clock.advance(10)
    acts = svc.reserve_out_tick()
    assert sent(acts) == ["back"] and svc.rsv_get("rsv_out")["step"] == "relay", acts
    texts = [n for n in notices(acts)]
    assert len(texts) == 1 and texts[0].force_sound and rc.HOST_A in texts[0].text and OWN_IP in texts[0].text


def test_a_merge_step_that_outlives_its_term_asks_for_the_replica_again(away):
    svc = away.svc
    _at_step(away, "merge", outbound.RELEASED_WAIT_S)
    away.clock.advance(outbound.RELEASED_WAIT_S - 5)
    assert svc.reserve_out_tick() == []
    away.clock.advance(10)
    assert svc.reserve_out_tick() == [("send", "back_req", {})]
    assert svc.rsv_get("rsv_out")["step"] == "back_req"


def test_a_restart_in_the_middle_of_the_merge_asks_for_the_replica_again(away):
    svc = away.svc
    _at_step(away, "merge", outbound.RELEASED_WAIT_S)
    assert svc.reserve_return_after_restart() is False, "Telegram забирать рано: слияния не было"
    assert svc.rsv_get("rsv_out")["step"] == "back_req"
    away.clock.advance(2)
    assert svc.reserve_out_tick() == [("send", "back_req", {})], "после рестарта реплику не попросили снова"


def test_the_return_drops_the_reconnect_notice_mark(away):
    """«Переподключи VPN» — один раз на переезд: после возврата следующему
    переезду метка не должна остаться."""
    svc = away.svc
    svc.db.set_state("move_notified", "1")
    _at_step(away, "relay")
    svc.reserve_wiped_in()
    assert not svc.db.get_state("move_notified"), "после возврата клиентам не скажут переподключиться в следующий раз"


# ── слияние: поколение снимка и строки о правках ────────────────────────────

@pytest.mark.parametrize("out_gen", [0, 5])
def test_the_merge_finds_the_snapshot_by_the_generation_of_the_replica(merge_world, out_gen):
    """Поколение переезда не записано (переезд мимо основы) или снимка с ним
    нет — снимок берётся по поколению, с которого стартовал гость."""
    w = merge_world
    w.svc.rsv_update("rsv_out", gen=out_gen)
    acts = w.svc.reserve_released_in(rm.snapshot_blob(w.svc, gen=7))
    assert acts == [("restart",)], f"gen={out_gen}: снимок base-7 не найден — {w.svc.rsv_get('rsv_out')}"


def test_without_a_generation_anywhere_the_merge_fails_with_the_reason(merge_world):
    w = merge_world
    w.svc.rsv_update("rsv_out", gen=0)
    w.svc.reserve_released_in(rm.snapshot_blob(w.svc, gen=0))
    assert w.svc.reserve_out() == "away" and "base-0" in w.svc.rsv_get("rsv_out")["merge_error"]


def test_a_failed_merge_resets_the_health_and_marks_the_switch(merge_world):
    """Ошибка слияния — снова away: демпфер возврата считается заново, иначе
    следующий возврат начнётся тем же тактом."""
    w = merge_world
    w.svc.rsv_update("rsv_out", healthy_mono=5.0, healthy_since=timeutil.now_iso(), can_return=True, last_switch="")
    w.svc.reserve_released_in(rm.snapshot_blob(w.svc, k=b"\x02" * 32))
    out = w.svc.rsv_get("rsv_out")
    assert out["state"] == "away" and out.get("last_switch"), out
    assert (out.get("healthy_mono"), out.get("healthy_since"), out.get("can_return")) == (0, "", False), out


def test_a_telegram_account_changed_on_the_reserve_is_named_in_the_report(merge_world):
    w = merge_world
    anna = w.anna.id

    def edit(db):
        db.activate_client(anna, 7999)
    w.svc.reserve_released_in(rm.snapshot_blob(w.svc, edit))
    lost = w.svc.rsv_get("rsv_out")["merge_report"]["lost"]
    line = [x for x in lost if "Telegram" in x]
    assert line and "Аня" in line[0] and "7001" in line[0] and "7999" in line[0], lost


def test_an_untouched_base_gives_no_warning_lines(merge_world):
    w = merge_world
    w.svc.reserve_released_in(rm.snapshot_blob(w.svc))
    assert w.svc.rsv_get("rsv_out")["merge_report"]["lost"] == []


def test_a_device_added_to_the_admin_profile_on_the_reserve_is_named_in_the_report(merge_world, make_active_client):
    w = merge_world
    adm = make_active_client(name="Админ", tg_id=int(config.ADMIN_ID))
    rm.put_base(w.svc, gen=7)

    def edit(db):
        db.create_device(adm.id, "Ноут чужой", "DUMMY-pub-adm", "DUMMY-psk", "10.8.1.78")
    w.svc.reserve_released_in(rm.snapshot_blob(w.svc, edit))
    lost = w.svc.rsv_get("rsv_out")["merge_report"]["lost"]
    assert [x for x in lost if "админа" in x and "Ноут чужой" in x], lost


# ── мимо основы и проверка записи в away ────────────────────────────────────

def test_the_record_watch_is_silent_while_the_reserve_holds_the_right_to_answer(mv, monkeypatch):
    """Т-РУ16 («запись указывает не сюда») при праве отвечать у резерва —
    ложная тревога: переезд был, запись ушла законно."""
    svc = mv.svc
    from awgbot.domain.services.reserve import dns as dnsmod
    monkeypatch.setattr(dnsmod.random, "uniform", lambda a, b: 1.0)
    mv.dns.auth = [PEER_IP]
    svc.reserve_set_poll("away")
    mv.clock.advance(10 ** 5)
    assert svc.reserve_record_watch_tick() == [], "Т-РУ16 при праве отвечать у резерва"
    reads = mv.dns.auth_reads
    svc.reserve_set_poll("mine")
    mv.clock.advance(10 ** 5)
    svc.reserve_record_watch_tick()
    assert mv.dns.auth_reads > reads, "контроль: при своём праве отвечать запись проверяется"


# ── отметки monotonic из прежней загрузки ───────────────────────────────────

def test_a_step_deadline_from_a_previous_boot_counts_as_expired(away):
    """После перезагрузки monotonic начинается с нуля: срок, записанный до
    неё, оказывается на годы в будущем — шаг не должен висеть вечно."""
    svc = away.svc
    _at_step(away, "merge", outbound.RELEASED_WAIT_S)
    svc.rsv_update("rsv_out", deadline=away.clock.t + 400 * 86400)
    assert svc.reserve_out_tick() == [("send", "back_req", {})], "срок прежней загрузки держит шаг вечно"


def test_a_probe_mark_from_a_previous_boot_does_not_block_the_record_probe(away):
    svc = away.svc
    _at_step(away, "record_back", outbound.RECORD_WAIT_S, record_set_at="", record_probe=away.clock.t + 10 ** 6)
    assert sent(svc.reserve_out_tick()) == ["back"], "отметка пробы из будущего заморозила проверку записи"


# ── сторож обновления и восстановления ──────────────────────────────────────

def _busy(capsys) -> tuple[int, str]:
    from tools import rsvbusy
    rc_ = rsvbusy.main()
    return rc_, capsys.readouterr().out.strip()


@pytest.mark.parametrize("out, inn, want", [
    ("home", "idle", 0), ("leaving", "idle", 3), ("away", "idle", 3), ("returning", "idle", 3),
    ("home", "taking", 3), ("home", "serving", 3), ("home", "releasing", 3), ("home", "relay", 0),
])
def test_the_busy_guard_refuses_while_clients_are_moving_or_hosted(mv, capsys, out, inn, want):
    svc = mv.svc
    svc.reserve_set_out(out)
    svc.reserve_set_in(inn)
    code, text = _busy(capsys)
    assert code == want, f"out={out} in={inn}: код {code} ({text})"
    if want == 3:
        assert "Beta" in text and "\n" not in text, f"причина не одной строкой или без имени резерва: {text!r}"


def test_the_busy_guard_is_free_without_a_pair(services, rsv, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    services.reserve_set_out("away")                         # следы без пары не мешают обновлению
    assert _busy(capsys)[0] == 0


def test_the_busy_guard_is_free_without_a_base(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "нет")
    assert _busy(capsys)[0] == 0


# ── отказ действий над хостом, пока здесь клиенты резерва ───────────────────

@pytest.mark.parametrize("inn, refused", [("taking", True), ("serving", True), ("releasing", True),
                                          ("relay", False), ("idle", False)])
def test_host_actions_are_refused_while_the_reserves_clients_are_here(mv, inn, refused):
    from awgbot.domain.services.types import ServiceError
    mv.svc.reserve_set_in(inn)
    if refused:
        with pytest.raises(ServiceError, match="Beta"):
            mv.svc.reserve_refuse_if_away()
    else:
        mv.svc.reserve_refuse_if_away()


# ── новое поколение и пакет ─────────────────────────────────────────────────

def test_a_new_generation_asks_for_a_fresh_package_at_once(mv):
    svc = mv.svc
    svc.__dict__.pop("_rsv_pkg_due_at", None)
    svc.reserve_new_generation("тест")
    assert _pkg_due(svc), "новое поколение без замены пакета — у резерва доли прежнего"


def test_the_generations_are_part_of_the_package_fingerprint(mv):
    """Пакет по таймеру без изменений не уходит (логический отпечаток); смена
    поколения M — изменение, даже если база та же."""
    svc = mv.svc
    assert svc.reserve_pkg_build(force=True) is not None
    assert svc.reserve_pkg_build() is None, "контроль: без изменений пакет не собирается"
    keys = svc.rsv_get("rsv_keys")
    svc.rsv_put("rsv_keys", {**keys, "m_gen": int(keys.get("m_gen") or 0) + 1})
    assert svc.reserve_pkg_build() is not None, "смена поколения M не дала нового пакета"


# ── удаление пары ───────────────────────────────────────────────────────────

def test_unpairing_drops_the_peers_package_and_the_merge_snapshots(mv, tmp_path):
    svc = mv.svc
    rm.put_base(svc, gen=7)
    pkg = tmp_path / "reserve" / "guest.pkg"
    pkg.write_bytes(b"DUMMY-package")
    svc.rsv_put("rsv_pkg_in", {"gen": 3, "fp": "DUMMY-fp", "share": "DUMMY"})
    svc.reserve_unpair("button")
    assert not pkg.exists(), "пакет второй стороны пережил удаление пары"
    assert not (tmp_path / "reserve" / "out").exists(), "снимки base-* с базой клиентов остались"


# ── собственный адрес при сопряжении ────────────────────────────────────────

def test_the_own_address_is_pinned_from_the_authoritative_answer_first(services, rsv, monkeypatch):
    """Системный резолвер может держать кэш (или уже адрес резерва): свой
    адрес — по ответу авторитетного сервера записи."""
    from awgbot.util import dnsapi
    settings.set_value("app.network.self_host", "")
    settings.set_value("app.network.server_host", rc.HOST_A)
    monkeypatch.setattr(dnsapi, "read_authoritative", lambda name: ["2001:db8::1", OWN_IP])
    monkeypatch.setattr(dnsapi, "read_system", lambda name, timeout=3.0: [PEER_IP])
    assert services.reserve_pin_self_host() == OWN_IP
    assert settings.get("app.network.self_host") == OWN_IP, "адрес не записан — при переезде линк уйдёт за именем"
    monkeypatch.setattr(dnsapi, "read_authoritative", lambda name: [PEER_IP])
    assert services.reserve_pin_self_host() == OWN_IP, "заданный адрес перезаписан новым резолвом"


def test_without_an_authoritative_answer_the_system_resolver_is_asked(services, rsv, monkeypatch):
    from awgbot.util import dnsapi

    def down(name):
        raise dnsapi.DnsError("авторитетный сервер не ответил")
    settings.set_value("app.network.self_host", "")
    settings.set_value("app.network.server_host", rc.HOST_A)
    monkeypatch.setattr(dnsapi, "read_authoritative", down)
    monkeypatch.setattr(dnsapi, "read_system", lambda name, timeout=3.0: [OWN_IP])
    assert services.reserve_pin_self_host() == OWN_IP


def test_an_unresolvable_name_pins_nothing_and_does_not_fail(services, rsv, monkeypatch):
    from awgbot.util import dnsapi
    settings.set_value("app.network.self_host", "")
    settings.set_value("app.network.server_host", rc.HOST_A)
    monkeypatch.setattr(dnsapi, "read_authoritative", lambda name: [])
    monkeypatch.setattr(dnsapi, "read_system", lambda name, timeout=3.0: [])
    assert services.reserve_pin_self_host() == ""
    assert not settings.get("app.network.self_host")
