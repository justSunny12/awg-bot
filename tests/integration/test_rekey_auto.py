"""I1 живым линкам — смена ключей без вопроса (`auto=i1`): без приглашения
в чате бота шлюза, `ready` сразу,
фиксация по hello новым ключом; не встало — прежние ключи, повтор через
сутки, после трёх — строка карточки; агент без `rekey2` — попыток нет; ручная
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
    assert server.gateway_screen_state(1, lazy_ping=False)["i1"] == T_L1, "строки «не встало трижды» нет в карточке"


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


# ── правки по ревью (FIXES-uupd-2: i1-1…i1-4 и вне оси) ──────────────────────

def _switched(server, vps):
    """Попытка ради I1 дошла до переключения ВПС: шлюзовая сторона уже с I1."""
    started, _ = server.gateway_i1_tick()
    assert started == [1]
    rid = server.gateway_rekey(1)["id"]
    action, _ = server.gateway_rekey_ack_in(1, {"id": rid, "state": "ready"})
    assert action == "go"
    server.gateway_rekey_go(1)
    assert server.gateway_rekey_commit(1)[0] is True
    assert wgconf.parse(vps.gw.read_text(encoding="utf-8")).get("Interface", "I1") == I1
    return rid


def test_a_failure_after_the_switch_counts_as_a_try(server, vps, clock):
    """i1-1: в окне switched (до hello новым ключом) у шлюзовой стороны уже
    новый I1 — такт не сравнивает и план не стирает; возврат через 180 с —
    неудача: tries=1, повтор через сутки."""
    _due(server, clock)
    rid = _switched(server, vps)
    assert server.gateway_i1_tick()[0] == []
    plan = server.gateway_i1_plan(1)
    assert plan.get("rid") == rid and plan["tries"] == 0, "план стёрт до фиксации"
    clock.at(clock.shift + 200)
    server.gateway_rekey_tick()                               # 180 с без hello — прежние ключи
    assert server.gateway_rekey(1) == {}
    server.gateway_i1_tick()
    plan = server.gateway_i1_plan(1)
    assert plan["tries"] == 1 and not plan.get("rid"), plan
    from awgbot.util import timeutil
    wait = timeutil.elapsed(timeutil.now(), timeutil.parse_iso(plan["next_at"]))
    assert 24 * HOUR * 0.6 - 5 <= wait <= 24 * HOUR * 1.4 + 5


def test_the_slot_is_on_i1_only_after_the_hello(server, vps, clock, caplog):
    """Журнал «слот на I1» и снятие плана — только после фиксации."""
    import logging
    _due(server, clock)
    _switched(server, vps)
    with caplog.at_level(logging.INFO):
        server.gateway_i1_tick()
        assert "на I1 клиентского интерфейса" not in caplog.text
        assert server.gateway_rekey_hello(1, vps.session_key())["rekeyed"]
        server.gateway_i1_tick()
    assert server.gateway_i1_plan(1) == {} and "на I1 клиентского интерфейса" in caplog.text


def test_a_refused_stage_is_a_failure_too(server, clock, monkeypatch):
    """i1-2: стадия отказала — неудача в счёт (иначе строка карточки не появится никогда)."""
    from awgbot.domain.services.types import ServiceError
    _due(server, clock)

    def boom(slot_id, auto=""):
        raise ServiceError("Новые ключи не сгенерированы: DUMMY")
    monkeypatch.setattr(server, "gateway_rekey_stage", boom)
    for n in (1, 2, 3):
        assert server.gateway_i1_tick()[0] == []
        assert server.gateway_i1_plan(1)["tries"] == n
        clock.at(clock.shift + 24 * HOUR * 1.4 + 10)
    from awgbot.domain.services.gateway_link.rekey_i1 import T_L1
    assert server.gateway_i1_line(1) == T_L1


def test_the_plan_goes_with_the_device(server):
    """i1-3: план I1 — ключ устройства: замена устройства слота неудач прежнего
    не наследует."""
    from awgbot.domain.services.gateway_link import slotkeys
    assert slotkeys.KEYS["gwlink_i1"].when == slotkeys.DEVICE
    server._i1_plan_save(1, {"want": I1, "tries": 3, "next_at": "2026-10-01T00:00:00+03:00", "rid": "x"})
    server.gwlink_forget(1)
    assert server.gateway_i1_plan(1) == {}


def test_no_attempt_while_an_update_command_is_in_flight(server, clock):
    """i1-4: команда обновления шлюзу вне хода ещё в пути — тоже не начинаем."""
    server.gateway_i1_tick()
    clock.at(7 * HOUR)
    server.fleet_slot_set(1, {"state": "sent", "tag": "v3.4.1"})
    assert server.gateway_i1_tick()[0] == [] and server.gateway_rekey(1) == {}
    server.fleet_slot_set(1, {"state": "done", "tag": "v3.4.1"})
    assert server.gateway_i1_tick()[0] == [1]


def test_a_failing_client_interface_read_is_not_retried_every_tick(server, monkeypatch):
    """Неудача чтения клиентского I1 кешируется: такт не зовёт exec каждые 30 с;
    ручная смена ключей читает заново."""
    calls = {"n": 0}

    def boom(**_):
        calls["n"] += 1
        raise awg.AwgError("нет интерфейса")
    monkeypatch.setattr(awg, "read_server_params", boom)
    for _ in range(5):
        assert server.gateway_i1_tick() == ([], [])
    assert calls["n"] == 1, "такт читал неотвечающий интерфейс каждый раз"
    assert server.gateway_client_i1() == (False, "") and calls["n"] == 2


def test_t_l2_only_when_a_link_really_has_no_i1(server, vps, monkeypatch):
    """«У клиентов нет I1» — одним уведомлением без звука и только когда у линка I1
    фактически нет; пока у линка стоит прежний — молчим (его снимет смена)."""
    from awgbot.domain.services.gateway_link.rekey_i1 import T_L2
    monkeypatch.setattr(awg, "read_server_params", lambda force=False, iface=None: {"obfuscation": {"I1": ""}})
    monkeypatch.setattr(server, "gateway_link_i1", lambda gw: (True, I1))
    assert server.gateway_i1_tick()[1] == [], "уведомление «нет I1» при стоящем у линка I1"
    monkeypatch.setattr(server, "gateway_link_i1", lambda gw: (True, ""))
    notes = server.gateway_i1_tick()[1]
    assert [n.text for n in notes] == [T_L2] and notes[0].silent
    assert server.gateway_i1_tick()[1] == []
    assert server.gateway_i1_line(1) is None, "«нет I1» строкой карточки"


def test_the_agent_keeps_quiet_about_auto_rekeys(svc, host, monkeypatch):
    """При auto агент в чат не пишет: «запрос отозван», «не поднялись» — о
    запросе, которого человек не видел; только журнал."""
    import time as _time
    conf, _, _ = _next_conf(host)
    svc.rekey_next_in({**_msg(conf), "auto": "i1"})
    st = svc.rekey_state()
    assert st["state"] == "ready" and st.get("auto") == "i1"
    res = svc.rekey_cancel({"id": st["id"]})
    assert res is not None and res.get("auto") == "i1"
    svc.rekey_next_in({**_msg(conf, "b1b2c3d4e5f6"), "auto": "i1"})
    st = svc.rekey_state()
    st["state"] = "switched"
    st["switched_at"] = _time.time() - 3600
    svc._rk_save(st)
    monkeypatch.setattr(svc, "_rk_apply_file", lambda name: True)
    assert svc.rekey_tick()["note"] == "", "«не поднялись» в чат при auto"
    svc._rk_save({**st, "state": "ready", "at": _time.time() - 10 * 24 * 3600})
    assert svc.rekey_tick()["note"] == "", "«запрос отозван» в чат при auto"


async def test_the_client_says_nothing_on_an_auto_cancel_or_failed_go(monkeypatch):
    from awgbot.runtime import linkclient
    from awgbot.runtime.linkclient import rekey as lrk
    said: list = []

    class _S:
        def rekey_cancel(self, msg):
            return {"msg": None, "auto": "i1"}

        def rekey_go(self, msg):
            return {"ok": False, "error": "x", "msg": None, "auto": "i1"}

    class _C(lrk.RekeyMixin):
        services = _S()
        _writer = None

        async def _send(self, kind, body, pad=0):
            return True

    async def notify(text, **kw):
        said.append(text)
    monkeypatch.setattr(linkclient, "_notify", notify)
    monkeypatch.setattr(linkclient, "_edit", None)
    c = _C()
    await c._rekey_cancel({"id": "x"})
    await c._rekey_go({"id": "x"})
    assert said == []
