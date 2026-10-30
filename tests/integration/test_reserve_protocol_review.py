"""Протокол пары после ревью: пара подтверждается первой сессией на новых
ключах и снимается, если за 10 минут её не было (session.py); пакет
досылается, когда резерв называет не тот (или никакой) пакет, но не во время
отъезда (package.py); удаление пары снимает зону в любой фазе (unpair.py);
копия ждёт конца сопряжения пары (backups.py); матрица готовности знает, чего
не хватает зоне (compat.zone_why), и есть ли у резерва пакет.

Цена ошибки: неподтверждённая пара навсегда — у одной стороны пара, у другой
приглашение, резерва нет, а карточка говорит «в паре»; сессия, не
сбросившая отметку slots, — стороны зоны после восстановления резерва не
переиздаются; резерв после восстановления без чужого пакета считается
готовым — переезд упирается в «пакета нет» в момент аварии; удаление пары
при обслуживании — клон и бот второй стороны живут без автомата; копия
посреди сопряжения — ключ стороны принявшего в копии, которой нет пары;
зона без dnsmasq «готова» — пользователи второй стороны без DNS при переезде.
"""
from __future__ import annotations

import json

import pytest

from awgbot.domain.services.reserve import compat
from awgbot.infra import rsvzone
from awgbot.util import timeutil

from tests.integration import _reserve_common as rc
from tests.integration.test_reserve_package import _ack, _tick, clock, paired

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["clock", "paired"]

REAL_ZONE_WHY = compat.zone_why                               # до подмены общей фикстурой


def _ago(seconds: float) -> str:
    return timeutil.to_iso(timeutil.shift(timeutil.now(), -seconds))


# ── подтверждение пары ──────────────────────────────────────────────────────

@pytest.fixture()
def fresh_pair(services, rsv, monkeypatch):
    rc.pair_row(services)
    monkeypatch.setattr(rsvzone, "teardown", lambda: "")
    monkeypatch.setattr(rsvzone, "wipe", lambda: "")
    services.rsv_update("rsv_peer_state", linked_at=_ago(30), confirmed=False)
    return services


def test_an_unconfirmed_pair_is_kept_for_ten_minutes(fresh_pair):
    assert fresh_pair.reserve_pair_confirm_tick() == [] and fresh_pair.reserve_paired()


def test_a_pair_without_a_session_for_ten_minutes_is_taken_away_with_one_notice(fresh_pair):
    fresh_pair.rsv_update("rsv_peer_state", linked_at=_ago(601))
    notes = fresh_pair.reserve_pair_confirm_tick()
    assert not fresh_pair.reserve_paired(), "пара без единой сессии на новых ключах осталась"
    assert len(notes) == 1 and "Beta" in notes[0].text, [n.text for n in notes]
    assert not notes[0].text.rstrip().endswith("."), "уведомления без точки в конце"
    assert fresh_pair.reserve_pair_confirm_tick() == [], "второе уведомление о той же паре"


def test_the_first_session_confirms_the_pair_and_resends_the_zone_sides(fresh_pair):
    fresh_pair.rsv_update("rsv_view", slots_sent=[1, 2], slots_at=timeutil.now_iso())
    fresh_pair.reserve_session_opened({})
    assert fresh_pair.reserve_peer_state().get("confirmed") is True
    view = fresh_pair.rsv_get("rsv_view")
    assert (view.get("slots_sent"), view.get("slots_at")) == ([], ""), "стороны зоны не переиздаются новой сессией"
    fresh_pair.rsv_update("rsv_peer_state", linked_at=_ago(3600))
    assert fresh_pair.reserve_pair_confirm_tick() == [] and fresh_pair.reserve_paired()


def test_a_pair_from_before_this_rule_is_confirmed(services, rsv):
    """Пара прежнего выпуска: ни linked_at, ни confirmed — снимать её нельзя."""
    rc.pair_row(services)
    assert services.reserve_pair_confirm_tick() == [] and services.reserve_paired()


# ── пакет у резерва ─────────────────────────────────────────────────────────

def test_a_reserve_without_any_package_after_a_restore_gets_it_again(paired, make_active_client, clock):
    make_active_client(name="Профиль", tg_id=7001)
    built = _tick(paired, clock)
    _ack(paired, built)
    paired.rsv_put("rsv_peer_state", {"session": True, "pkg": {}})       # резерв восстановлен из копии
    paired.reserve_pkg_resend_if_stale()
    assert paired.__dict__.get("_rsv_pkg_due_at") is not None, "резерв без пакета — основа обязана прислать заново"


def test_no_resend_while_my_clients_are_away(paired, make_active_client, clock):
    make_active_client(name="Профиль", tg_id=7001)
    _ack(paired, _tick(paired, clock))
    paired.reserve_set_out("away")
    paired.rsv_put("rsv_peer_state", {"session": True, "pkg": {"fp": "00" * 32}})
    paired.reserve_pkg_resend_if_stale()
    assert paired.__dict__.get("_rsv_pkg_due_at") is None, "пакет пересобран, пока пользователи у резерва"


def test_the_st_of_the_reserve_triggers_the_resend(paired, make_active_client, clock):
    from tests.integration import _reserve_move as rm
    make_active_client(name="Профиль", tg_id=7001)
    _ack(paired, _tick(paired, clock))
    paired.reserve_st_in(rm.peer_st(pkg={"gen": 0, "fp": ""}))
    assert paired.__dict__.get("_rsv_pkg_due_at") is not None, "st без пакета не заставил прислать его"


@pytest.mark.parametrize("rec, file, want", [(True, True, True), (False, True, False), (True, False, False)])
def test_the_package_of_the_other_side_is_present_only_with_the_record_and_the_file(services, rec, file, want):
    """Доли в пакете нет: «пакет на месте» — запись о принятом поколении и файл; ящик
    кода поколения проверяет reserve_open_block."""
    from awgbot.domain.services.reserve.package import guest_pkg_path
    import os
    services.rsv_put("rsv_pkg_in", {"gen": 3, "fp": "ab", "m_gen": 1} if rec else None)
    path = guest_pkg_path()
    if file:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").write(b"DUMMY")
    elif os.path.exists(path):
        os.unlink(path)
    assert services.reserve_pkg_in_present() is want


# ── удаление пары снимает зону ──────────────────────────────────────────────

@pytest.mark.parametrize("phase, down", [("taking", True), ("serving", True), ("relay", True), ("idle", False), ("", False)])
def test_unpairing_takes_the_zone_down_in_any_live_phase(services, rsv, monkeypatch, phase, down):
    rc.pair_row(services)
    calls = []
    monkeypatch.setattr(rsvzone, "teardown", lambda: calls.append("teardown") or "")
    monkeypatch.setattr(rsvzone, "wipe", lambda: calls.append("wipe") or "")
    rsvzone.STATE_DIR.mkdir(parents=True, exist_ok=True)
    rsvzone.STATE_PATH.write_text(json.dumps({"phase": phase}), encoding="utf-8")
    services.reserve_unpair()
    assert (calls[:2] == ["teardown", "wipe"]) is down, f"{phase}: {calls}"
    if not down:
        assert calls == []


# ── копия ждёт сопряжения пары ──────────────────────────────────────────────

def test_the_backup_waits_while_a_pair_invitation_is_live(services, monkeypatch):
    monkeypatch.setattr(services, "reserve_invite_live", lambda: True)
    busy = services.backup_rekey_busy()
    assert busy is not None and "второй сервер" in str(busy), busy
    monkeypatch.setattr(services, "reserve_invite_live", lambda: False)
    assert services.backup_rekey_busy() is None


# ── чего не хватает зоне ────────────────────────────────────────────────────

@pytest.fixture()
def plan(monkeypatch):
    got = {"dnsmasq": "yes", "python": "yes", "webhook_port": "443"}
    calls = []
    monkeypatch.setattr(rsvzone, "plan", lambda: calls.append(1) or dict(got))
    REAL_ZONE_WHY.__dict__.clear()
    yield got, calls
    REAL_ZONE_WHY.__dict__.clear()


@pytest.mark.parametrize("over, why", [
    ({}, ""),
    ({"dnsmasq": "no"}, "нет dnsmasq"),
    ({"python": "no"}, "нет python"),
    ({"webhook_port": "none"}, "нет свободного порта для вебхука"),
    ({"webhook_port": ""}, "нет свободного порта для вебхука"),
    ({"error": "скрипт зоны не запустился"}, "план зоны не прочитан"),
])
def test_the_zone_says_what_it_lacks(plan, over, why):
    got, _ = plan
    got.update(over)
    out = REAL_ZONE_WHY()
    assert (why in out) if why else out == "", out


def test_the_zone_plan_is_cached_for_ten_minutes(plan, monkeypatch):
    import time as _time
    got, calls = plan
    t = {"now": 1000.0}
    monkeypatch.setattr(_time, "monotonic", lambda: t["now"])
    REAL_ZONE_WHY()
    got["dnsmasq"] = "no"
    t["now"] += 599
    assert REAL_ZONE_WHY() == "" and len(calls) == 1, "план зоны — вызов скрипта на каждый st"
    t["now"] += 2
    assert "dnsmasq" in REAL_ZONE_WHY() and len(calls) == 2


def test_a_zone_that_lacks_something_closes_the_direction_towards_it():
    src = compat.side(name="Alpha", version="4.0.0", caps=list(compat.CAPS), gen=0, ports=[45871], ready=True,
                      has_gateway=True)
    dst = compat.side(name="Beta", version="4.0.0", caps=list(compat.CAPS), gen=0, ports=[45872], ready=True,
                      has_gateway=True, zone="на сервере нет dnsmasq — резолвер зоны не поднимется")
    ok, why = compat.direction_ready(src, dst)
    assert ok is False and "dnsmasq" in why and "Beta" in why, why
    assert compat.direction_ready(src, compat.side(**{**dict(name="Beta", version="4.0.0", caps=list(compat.CAPS),
                                                              gen=0, ports=[45872], ready=True, has_gateway=True)}))[0]


# ── пакет при занятой зоне ──────────────────────────────────────────────────

@pytest.mark.parametrize("state", ["taking", "serving", "releasing"])
def test_a_package_while_the_zone_hosts_is_refused_and_nothing_is_written(services, state):
    """Гость стартовал с прежнего пакета: новый на диске сбил бы поколение
    слияния при возврате — pkg_bad «зона занята», пакет и запись о нём не тронуты."""
    from awgbot.domain.services.reserve.package import guest_pkg_path
    import os
    services.rsv_put("rsv_pkg_in", {"gen": 3, "fp": "ab", "m_gen": 1})
    services.reserve_set_in(state)
    before = os.path.exists(guest_pkg_path())
    assert services.reserve_pkg_in(4, "cd", b"DUMMY-blob") == ("pkg_bad", "зона занята")
    assert services.rsv_get("rsv_pkg_in") == {"gen": 3, "fp": "ab", "m_gen": 1}
    assert os.path.exists(guest_pkg_path()) == before


# ── слово основы шлюзам при возврате ────────────────────────────────────────

@pytest.mark.parametrize("state, step, want", [
    ("returning", "back_req", "away"), ("returning", "merge", "away"), ("returning", "restart", "away"),
    ("returning", "take_tg", "away"), ("returning", "record_back", "away"), ("returning", "relay", "idle"),
    ("away", "", "away"), ("home", "", "idle"), ("leaving", "up", "idle"), ("leaving", "record", "away"),
])
def test_the_gateways_keep_forwarding_until_the_return_reaches_the_tail(services, state, step, want):
    """Пока пользователи на клоне (возврат до шага relay), шлюзам — away: снятая
    пересылка оставила бы их без РФ-части до конца возврата."""
    services.reserve_set_out(state, step=step)
    assert services.reserve_gw_state() == want, f"{state}/{step}"
