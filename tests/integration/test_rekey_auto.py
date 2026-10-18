"""I1 живым линкам — смена ключей без вопроса (`auto=i1`, §3.10 концепта
«единые обновления»): без приглашения в чате бота шлюза, `ready` сразу,
фиксация по hello новым ключом; не встало — прежние ключи, повтор через
сутки, после трёх — Т-Л1; агент без `rekey2` — попыток нет; ручная
«🔑 Ключи линка» — с вопросом, как была; по одному слоту, резервные первыми,
окно 30 мин…6 ч; ход набора идёт — не начинаем.

Сервер — настоящие Services со скриптом линка моделью (test_gwlink_rekey,
_Vps), агент — настоящий GatewayServices (test_link_txn_search_*); часы —
одни на обе стороны.
"""
from __future__ import annotations

import pytest

from awgbot.infra import awg
from awgbot.util import gwlink, wgconf
from tests.unit import _link_txn_common as _c
from tests.unit import test_gwlink_rekey as _vps
from tests.unit._link_txn_common import _msg, _next_conf

pytestmark = pytest.mark.integration

conf_dir, svc, clock, host = _c.conf_dir, _c.svc, _c.clock, _c.host
slot, vps = _vps.slot, _vps.vps

I1 = "<b 0xc70000000108ce1bc7e1d8a3b4a9><r 16><t><rc 8>"
HOUR = 3600


@pytest.fixture()
def server(services, slot, vps, monkeypatch):
    """Клиентский интерфейс с I1, агент слота 1 с rekey2; модель скрипта
    пишет LINK_I1 в шлюзовую сторону, как настоящий --rekey-stage."""
    base = awg.read_server_params
    monkeypatch.setattr(awg, "read_server_params", lambda force=False, iface=None: {
        **base(), "obfuscation": {**base()["obfuscation"], "I1": I1}})
    run = vps.run

    def stage_with_i1(mode, env=None):
        run(mode, env)
        if mode == "--rekey-stage" and (env or {}).get("LINK_I1"):
            nxt = vps._p(vps.gw, ".next")
            nxt.write_text(nxt.read_text(encoding="utf-8").replace("H4 = 4\n", f"H4 = 4\nI1 = {env['LINK_I1']}\n"),
                           encoding="utf-8")
    monkeypatch.setattr(services, "_run_link_script", stage_with_i1)
    real_open = open
    from awgbot.domain.services.gateway_link import linki1

    def _open(path, *a, **kw):
        p = str(path)
        if p.startswith("/root/"):
            p = str(vps.root / p[len("/root/"):])
        return real_open(p, *a, **kw)
    monkeypatch.setattr(linki1, "open", _open, raising=False)
    caps(services, 1)
    return services


def caps(services, n: int, *extra: str, rekey2: bool = True) -> None:
    services.gwlink_session_opened(n, "3.4.0", gwlink.PROTO,
                                   caps=("cfg1", "confirm1", "upd1") + (("rekey2",) if rekey2 else ()) + extra)


def _due(server, clock):
    """План составлен — момент его попытки: не раньше 30 минут и не позже 6 часов."""
    server.gateway_i1_tick()
    plan = server.gateway_i1_plan(1)
    from awgbot.util import timeutil
    wait = timeutil.elapsed(timeutil.now(), timeutil.parse_iso(plan["next_at"]))
    assert 30 * 60 - 5 <= wait <= 6 * HOUR + 5, wait
    assert server.gateway_rekey(1) == {}, "смена ключей раньше окна"
    clock.at(clock.shift + 6 * HOUR + 10)


def test_auto_i1_goes_without_a_question_and_commits_by_hello(server, vps, clock):
    _due(server, clock)
    started, _ = server.gateway_i1_tick()
    assert started == [1]
    body = server.gateway_rekey_next_body(1)
    assert body["auto"] == "i1" and f"I1 = {I1}" in body["conf"]
    action, notes = server.gateway_rekey_ack_in(1, {"id": body["id"], "state": "ready"})
    assert action == "go" and notes == []
    server.gateway_rekey_go(1)
    assert server.gateway_rekey_commit(1)[0] is True
    res = server.gateway_rekey_hello(1, vps.session_key())
    assert res["rekeyed"] and all(n.silent for n in res["notes"]), "итог смены без вопроса — со звуком"
    assert wgconf.parse(vps.gw.read_text(encoding="utf-8")).get("Interface", "I1") == I1
    server.gateway_i1_tick()
    assert server.gateway_i1_plan(1) == {}, "план пережил вставший I1"


def test_the_agent_takes_auto_i1_without_asking(svc, host):
    conf, _, _ = _next_conf(host)
    res = svc.rekey_next_in({**_msg(conf), "auto": "i1"})
    assert res["reply"]["state"] == "ready" and not res["ask"], "смена без вопроса спросила человека"
    assert svc.rekey_question() is None
    res = svc.rekey_next_in({**_msg(conf, "b1b2c3d4e5f6"), "auto": "evil"})
    assert res["ask"] and res["reply"]["state"] == "asked", "неизвестный auto — без вопроса"


def test_a_failed_attempt_returns_and_retries_daily_and_after_three_the_card_says_so(server, vps, clock):
    from awgbot.domain.services.gateway_link.rekey_i1 import T_L1
    _due(server, clock)
    for n in range(1, 4):
        started, _ = server.gateway_i1_tick()
        assert started == [1], n
        rid = server.gateway_rekey(1)["id"]
        _, notes = server.gateway_rekey_ack_in(1, {"id": rid, "state": "declined", "error": "сеть"})
        assert notes == [], "отказ смены без вопроса — уведомлением"
        server.gateway_i1_tick()
        plan = server.gateway_i1_plan(1)
        assert plan["tries"] == n and not plan.get("rid")
        from awgbot.util import timeutil
        wait = timeutil.elapsed(timeutil.now(), timeutil.parse_iso(plan["next_at"]))
        assert 24 * HOUR * 0.6 - 5 <= wait <= 24 * HOUR * 1.4 + 5
        assert server.gateway_i1_tick()[0] == [], "повтор раньше суток"
        server._rekey_save(1, {})                         # строка отказа прошла
        clock.at(clock.shift + 24 * HOUR * 1.4 + 10)
        if n < 3:
            assert server.gateway_i1_line(1) is None
    assert server.gateway_i1_line(1) == T_L1
    assert server.gateway_screen_state(1, lazy_ping=False)["i1"] == T_L1, "строки Т-Л1 нет в карточке"


def test_an_agent_without_rekey2_gets_no_attempts(server, clock):
    caps(server, 1, rekey2=False)
    server.gateway_i1_tick()
    clock.at(7 * HOUR)
    assert server.gateway_i1_tick()[0] == [] and server.gateway_rekey(1) == {}


def test_the_manual_rekey_still_asks(server):
    server.gateway_rekey_stage(1)
    body = server.gateway_rekey_next_body(1)
    assert "auto" not in body and f"I1 = {I1}" in body["conf"], "ручная смена без I1 или без вопроса"


def test_one_slot_at_a_time_reserves_first(server, vps, clock):
    _vps._second_slot(server)
    caps(server, 2)
    (vps.root / "gw-awglink2.conf").write_text(vps.gw.read_text(encoding="utf-8"), encoding="utf-8")
    server.db.set_state(server._RT_ACTIVE_KEY, "1")       # активный — слот 1, резервный — 2
    server.gateway_i1_tick()
    clock.at(7 * HOUR)
    staged: list = []
    real = server.gateway_rekey_stage

    def stage(slot_id, auto=""):
        staged.append(slot_id)
        return {"id": "x", "state": "staged"} if slot_id == 2 else real(slot_id, auto)
    server.gateway_rekey_stage = stage
    started, _ = server.gateway_i1_tick()
    assert started == [2], "первым не резервный"
    assert server.gateway_i1_tick()[0] == [] and staged == [2], "второй слот, пока первый не встал"


def test_no_attempt_while_the_fleet_runs(server, clock, monkeypatch):
    server.gateway_i1_tick()
    clock.at(7 * HOUR)
    monkeypatch.setattr(server, "fleet_running", lambda: True)
    assert server.gateway_i1_tick()[0] == [] and server.gateway_rekey(1) == {}
