"""Роль gateway: пробы, гистерезис, панель (docs/ROADMAP.md, п.7, этап 1)."""
from __future__ import annotations

import subprocess

import pytest

import awgbot.core.config as config
from awgbot.domain import gateway as gw
from awgbot.domain.gateway import GatewayServices, GwStatus, GwCheck
from awgbot.infra.db import Database


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "gw.db")
    d.init_schema()
    return d


@pytest.fixture()
def svc(db):
    return GatewayServices(db)


def _cp(rc=0, out=""):
    return subprocess.CompletedProcess([], rc, stdout=out.encode(), stderr=b"")


# ── роль в validate ──────────────────────────────────────────────────────────

def test_gateway_role_does_not_require_server_topology(monkeypatch):
    """Шлюз не выдаёт конфигов — требовать server_host/port у агента значило бы
    заставлять установщика выдумывать значения, которые никто не прочтёт."""
    monkeypatch.setattr(config, "BOT_TOKEN", "t")
    monkeypatch.setattr(config, "ADMIN_ID", 1)
    monkeypatch.setattr(config, "SERVER_HOST", "")
    monkeypatch.setattr(config, "SERVER_PORT", None)
    monkeypatch.setattr(config, "ROLE", "gateway")
    config.validate()                                    # не поднимает

    monkeypatch.setattr(config, "ROLE", "client")
    with pytest.raises(RuntimeError):
        config.validate()


def test_unknown_role_fails_loudly(monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", "t")
    monkeypatch.setattr(config, "ADMIN_ID", 1)
    monkeypatch.setattr(config, "ROLE", "gatewy")
    with pytest.raises(RuntimeError):
        config.validate()


# ── покрытие ядер ────────────────────────────────────────────────────────────

def test_kernel_coverage_reports_kernels_without_module(svc, tmp_path):
    """Дыра, найденная руками: dkms молча пропускает ядро без headers, и ребут
    в него оставляет шлюз без awg. Агент обязан видеть это заранее — но только
    для ядер, в которые машина реально загрузится: тот же вариант платы и не
    старее запущенного. Чужие платы (-2712 на Pi 4) и старые ядра — не шум."""
    for k, with_mod in (("6.18.39+rpt-rpi-v8", True),      # запущенное
                        ("6.18.44+rpt-rpi-v8", False),     # новое из apt — ДЫРА
                        ("6.18.34+rpt-rpi-v8", False),     # старое — назад не идём
                        ("6.18.44+rpt-rpi-2712", False),   # другая плата — никогда
                        ("6.18.39+rpt-rpi-v7l", False)):
        d = tmp_path / k / "updates" / "dkms"
        d.mkdir(parents=True)
        if with_mod:
            (d / "amneziawg.ko.xz").write_bytes(b"x")
    missing, total = svc.kernel_coverage(modules_root=str(tmp_path),
                                         running="6.18.39+rpt-rpi-v8")
    assert total == 2
    assert missing == ["6.18.44+rpt-rpi-v8"]


def test_kernel_coverage_running_without_module_is_reported(svc, tmp_path):
    (tmp_path / "6.18.39+rpt-rpi-v8").mkdir()
    missing, total = svc.kernel_coverage(modules_root=str(tmp_path),
                                         running="6.18.39+rpt-rpi-v8")
    assert (missing, total) == (["6.18.39+rpt-rpi-v8"], 1)


# ── гистерезис ───────────────────────────────────────────────────────────────

def test_streak_alert_arms_after_n_and_disarms_after_n(svc):
    """Алерт после N плохих ПОДРЯД, отбой после N хороших; None не двигает
    счётчики — «не смог посмотреть» не равно ни норме, ни отказу."""
    fire = lambda bad: svc._streak_alert("t", bad, 3, "ПЛОХО", "ОК")
    assert fire(True) == [] and fire(True) == []
    assert fire(None) == [], "None сдвинул счётчик"
    notes = fire(True)
    assert len(notes) == 1 and notes[0].text == "ПЛОХО"
    assert notes[0].force_sound is True, "алерт шлюза обязан быть громким"
    assert fire(True) == [], "повторный алерт при уже взведённом"
    assert fire(False) == [] and fire(False) == []
    notes = fire(False)
    assert len(notes) == 1 and notes[0].text == "ОК"
    assert fire(False) == []


def test_one_good_measurement_resets_the_bad_streak(svc):
    fire = lambda bad: svc._streak_alert("t2", bad, 3, "ПЛОХО", "ОК")
    fire(True); fire(True); fire(False)
    assert fire(True) == [] and fire(True) == [], "серия не сбросилась"


# ── пробы ────────────────────────────────────────────────────────────────────

def test_versions_parses_modinfo(svc, monkeypatch):
    monkeypatch.setattr(gw, "_run", lambda a, timeout=10: _cp(0,
        "filename: /x\nversion:        3.1.20260812\nsrcversion:     ABCDEF\n"))
    assert svc.versions() == ("3.1.20260812", "ABCDEF")


def test_link_status_reads_freshest_handshake(svc, monkeypatch):
    import awgbot.util.timeutil as tu
    now = tu.now().timestamp()

    def run(argv, timeout=10):
        if argv[:3] == ["ip", "link", "show"]:
            return _cp(0)
        if "dump" in argv:                          # один вызов вместо двух
            return _cp(0, "PRIV\tPUB0\t0\toff\n"
                          f"PUB1\t(none)\t1.2.3.4:1\t10.9.1.0/24\t{int(now-40)}\t1000\t2000\t25\n")
        return _cp(1)

    monkeypatch.setattr(gw, "_run", run)
    up, age, rx, tx = svc.link_status()
    assert up and 35 <= age <= 60 and rx == 1000 and tx == 2000


_ALL_CHAINS = ("input", "tunnel_in", "forward", "postrouting", "output")


def _guard_json(sets: dict, chains=None, masq=("end0", "awg0")):
    chains = _ALL_CHAINS if chains is None else chains
    import json
    items = [{"metainfo": {}}]
    for name, elems in sets.items():
        items.append({"set": {"family": "inet", "name": name, "table": "awg_gw_guard",
                              "elem": list(elems)}})
    for c in chains:
        items.append({"chain": {"family": "inet", "table": "awg_gw_guard", "name": c}})
    for iface in masq:
        items.append({"rule": {"family": "inet", "table": "awg_gw_guard", "chain": "postrouting",
                               "expr": [{"match": {"op": "==", "left": {"meta": {"key": "oifname"}},
                                                   "right": iface}}, {"masquerade": None}]}})
    return json.dumps({"nftables": items})


def _guard_run(sets, chains=None, fwd_policy="accept", masq=("end0", "awg0")):
    """_run/subprocess-стаб: таблица awg_gw_guard в JSON, чужой FORWARD с политикой."""
    import json

    def run(argv, timeout=10, **kw):
        a = list(argv)
        if a[:1] == ["nft"]:
            a = a[1:]
        if a[:3] == ["-j", "list", "table"]:
            return _cp(0, _guard_json(sets, chains, masq))
        if a[:3] == ["-j", "list", "chain"]:
            return _cp(0, json.dumps({"nftables": [{"chain": {"name": "FORWARD", "policy": fwd_policy}}]}))
        if a[:2] == ["systemctl", "is-enabled"]:
            return _cp(0)
        if a[:3] == ["ip", "route", "show"]:
            return _cp(0, "default via 1.2.3.4 dev eth0\n")
        return _cp(0)
    return run


def test_plumbing_reads_the_guard_table(svc, monkeypatch):
    """Обвязка — одна nft-таблица; проверки читают её одним `nft -j list table`."""
    import subprocess as sp
    monkeypatch.setattr(config, "GW_CLIENT_SUBNET", "10.9.1.0/24")
    run = _guard_run({"tunnel_nets4": ["10.9.1.0/24", "10.99.99.0/30"],
                      "tg_nets4": list(GatewayServices.TG_RANGES)})
    monkeypatch.setattr(gw, "_run", run)
    monkeypatch.setattr(sp, "run", lambda argv, **kw: run(argv))
    monkeypatch.setattr(gw, "pathlib_read", lambda p: "1\n")
    checks = {c.name: c for c in svc.plumbing_checks()}
    assert checks["MASQUERADE/изоляция"].ok is True
    assert checks["цепочки таблицы"].ok is True
    assert checks["политика FORWARD"].ok is True
    assert checks["ip_forward"].ok is True
    assert "таблица awg_gw_guard" not in checks
    assert svc.tg_mark_missing(svc._guard_info) == []


def test_plumbing_flags_foreign_subnet_and_missing_table(svc, monkeypatch):
    import subprocess as sp
    monkeypatch.setattr(config, "GW_CLIENT_SUBNET", "10.9.1.0/24")
    run = _guard_run({"tunnel_nets4": ["10.8.1.0/24"], "tg_nets4": []}, chains=("input",))
    monkeypatch.setattr(gw, "_run", run)
    monkeypatch.setattr(sp, "run", lambda argv, **kw: run(argv))
    monkeypatch.setattr(gw, "pathlib_read", lambda p: "1\n")
    checks = {c.name: c for c in svc.plumbing_checks()}
    assert checks["MASQUERADE/изоляция"].ok is False, "бандл под другую подсеть"
    assert checks["цепочки таблицы"].ok is False
    # таблицы нет вовсе
    monkeypatch.setattr(sp, "run", lambda argv, **kw: _cp(1))
    checks = {c.name: c for c in svc.plumbing_checks()}
    assert checks["таблица awg_gw_guard"].ok is False
    assert svc.tg_mark_missing(None) == list(GatewayServices.TG_RANGES)


def test_masquerade_check_disabled_without_subnet_is_unknown_not_ok(svc, monkeypatch):
    """Без gateway.client_subnet проверка выключена — ⚪ «нечем проверить», а не ✅."""
    import subprocess as sp
    monkeypatch.setattr(config, "GW_CLIENT_SUBNET", "")
    run = _guard_run({"tunnel_nets4": ["10.9.1.0/24"], "tg_nets4": []})
    monkeypatch.setattr(gw, "_run", run)
    monkeypatch.setattr(sp, "run", lambda argv, **kw: run(argv))
    monkeypatch.setattr(gw, "pathlib_read", lambda p: "1\n")
    checks = {c.name: c for c in svc.plumbing_checks()}
    assert checks["MASQUERADE/изоляция"].ok is None


def test_docker_drop_policy_on_forward_is_reported(svc, monkeypatch):
    """accept в нашей таблице не отменяет drop в чужой: docker-овский DROP на
    FORWARD молча перекрыл бы транзит клиентов."""
    import subprocess as sp
    monkeypatch.setattr(config, "GW_CLIENT_SUBNET", "10.9.1.0/24")
    run = _guard_run({"tunnel_nets4": ["10.9.1.0/24"], "tg_nets4": []}, fwd_policy="drop")
    monkeypatch.setattr(gw, "_run", run)
    monkeypatch.setattr(sp, "run", lambda argv, **kw: run(argv))
    monkeypatch.setattr(gw, "pathlib_read", lambda p: "1\n")
    checks = {c.name: c for c in svc.plumbing_checks()}
    assert checks["политика FORWARD"].ok is False


# ── тик монитора ─────────────────────────────────────────────────────────────

def _quiet_status(**kw):
    st = GwStatus(link_up=True, handshake_age=10.0,
                  checks=[GwCheck("MASQUERADE", True)], temp=50.0, disk=30.0,
                  throttled={"raw": 0, "now": [], "ever": []},
                  module_version="v", srcversion="s", kernels_total=3, egress_ms=25.0)
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def test_dead_link_alerts_after_two_ticks(svc, monkeypatch):
    monkeypatch.setattr(svc, "status", lambda: _quiet_status(handshake_age=9999.0))
    assert svc.monitor_tick() == []
    notes = svc.monitor_tick()
    assert len(notes) == 1 and "Линк" in notes[0].text and notes[0].force_sound


def test_quiet_gateway_produces_no_notes_and_stores_snapshot(svc, monkeypatch):
    monkeypatch.setattr(svc, "status", lambda: _quiet_status())
    assert svc.monitor_tick() == []
    snap = svc.cached_status(60)
    assert snap is not None and snap.link_up is True and snap.handshake_age == 10.0
    assert snap.checks and snap.checks[0].name == "MASQUERADE", "проверки не пережили снимок"
    assert svc.cached_status(-1) is None, "устаревший снимок должен отвергаться"


# ── панель ───────────────────────────────────────────────────────────────────

def test_panel_renders_on_a_dead_gateway():
    """Панель обязана рисоваться и на полумёртвом шлюзе — именно тогда она
    нужнее всего."""
    from awgbot.bot import texts
    out = texts.gateway_panel(GwStatus())
    assert "лежит" in out and "Потребление за месяц" in out


# ── этап 2: операции ─────────────────────────────────────────────────────────

def test_apply_bundle_rejects_wrong_key_and_foreign_content(svc, monkeypatch, tmp_path):
    import base64, os
    from awgbot.util import bundlecrypt as bc
    mine = base64.b64encode(os.urandom(32)).decode()
    theirs = base64.b64encode(os.urandom(32)).decode()
    conf = tmp_path / "awglink.conf"
    conf.write_text("[Interface]\nPrivateKey = " + mine + "\n", encoding="utf-8")
    monkeypatch.setattr(config, "GW_LINK_CONF", str(conf))
    ran = []
    monkeypatch.setattr(gw, "_run", lambda a, timeout=10: ran.append(a) or _cp(0, "ok"))

    ok, msg = svc.apply_bundle(bc.encrypt(b"#__GW_SETUP_BELOW__\n__LINK_CONF_EOF__", theirs))
    assert not ok and "ключ" in msg and ran == [], "чужой бандл дошёл до запуска"

    ok, msg = svc.apply_bundle(bc.encrypt(b"#!/bin/sh\nrm -rf /\n", mine))
    assert not ok and "маркер" in msg and ran == [], "файл без контракта дошёл до запуска"


def test_apply_bundle_runs_our_bundle_and_removes_the_file(svc, monkeypatch, tmp_path):
    import base64, os
    from awgbot.util import bundlecrypt as bc
    mine = base64.b64encode(os.urandom(32)).decode()
    conf = tmp_path / "awglink.conf"
    conf.write_text("[Interface]\nPrivateKey = " + mine + "\n", encoding="utf-8")
    monkeypatch.setattr(config, "GW_LINK_CONF", str(conf))
    monkeypatch.setattr(gw.tempfile if hasattr(gw, "tempfile") else __import__("tempfile"),
                        "mkstemp", lambda **kw: (os.open(str(tmp_path / "b.sh"), os.O_RDWR | os.O_CREAT),
                                                 str(tmp_path / "b.sh")))
    ran = []
    monkeypatch.setattr(gw, "_run", lambda a, timeout=10: ran.append(a) or _cp(0, "Готово"))
    body = b"#!/bin/sh\n#__GW_SETUP_BELOW__\n__LINK_CONF_EOF__\n"
    ok, msg = svc.apply_bundle(bc.encrypt(body, mine))
    assert ok and "Готово" in msg
    assert ran and ran[0][:1] == ["sh"] and ran[0][2] == "--apply"
    assert not (tmp_path / "b.sh").exists(), "бандл с приватным ключом остался на диске"


def test_tg_mark_ensure_reasserts_the_table_once_per_interval(svc, monkeypatch):
    """Таблицу правит только скрипт: недостающие диапазоны → рестарт юнита,
    и не чаще раза в интервал — устаревший бандл не должен дёргать юнит каждый тик."""
    from awgbot.infra import gwguard
    calls = []
    monkeypatch.setattr(gwguard, "reassert", lambda: (calls.append(1), (True, ""))[1])
    svc._last_reassert = 0.0
    assert svc.tg_mark_ensure(["149.154.160.0/20"]) == 1
    assert svc.tg_mark_ensure(["149.154.160.0/20"]) == 0, "второй раз подряд — ждём интервал"
    assert calls == [1]
    assert svc.tg_mark_ensure([]) == 0, "нечего чинить — юнит не трогаем"


# ── этап 3: самообновление — тот же механизм, что у клиентской роли ──────────

def test_gateway_confirms_applied_update_like_the_client_role(svc, monkeypatch):
    """Механизм общий (SelfUpdateMixin): после рестарта агент сверяет
    установленную версию с ожидаемой и отчитывается ровно один раз."""
    from awgbot.infra import updates
    monkeypatch.setattr(config, "INSTALLED_VERSION", "2.4.2")
    monkeypatch.setattr(updates, "release_body", lambda tag: "заметки")
    svc.db.set_state("update_pending", "v2.4.2")
    note = svc.confirm_applied_update()
    assert note is not None and "2.4.2" in note.text
    assert note.reply_markup is not None, "нет кнопки «В меню»"
    assert svc.confirm_applied_update() is None, "отчитался дважды"


def test_gateway_reports_update_that_did_not_apply(svc, monkeypatch):
    monkeypatch.setattr(config, "INSTALLED_VERSION", "2.4.1")
    svc.db.set_state("update_pending", "v2.4.2")
    note = svc.confirm_applied_update()
    assert note is not None and "2.4.1" in note.text and "2.4.2" in note.text


def test_tick_heals_uplink_policy_and_reports_once(svc, monkeypatch):
    """Пропавшее правило/маршрут аплинка перевыставляется в том же тике и
    об этом приходит одно уведомление — это событие, а не стрик."""
    monkeypatch.setattr(svc, "status", lambda: _quiet_status())
    fixes = iter([["правило по метке", "маршрут в аплинк"], []])
    monkeypatch.setattr(svc, "uplink_policy_heal", lambda: next(fixes))
    notes = svc.monitor_tick()
    assert len(notes) == 1 and "перевыставлена" in notes[0].text and not notes[0].critical
    assert svc.monitor_tick() == []


def test_plumbing_reports_uplink_policy(svc, monkeypatch):
    import subprocess as sp
    from awgbot.infra import gwguard
    monkeypatch.setattr(config, "GW_CLIENT_SUBNET", "10.9.1.0/24")
    run = _guard_run({"tunnel_nets4": ["10.9.1.0/24"], "tg_nets4": []})
    monkeypatch.setattr(gw, "_run", run)
    monkeypatch.setattr(sp, "run", lambda argv, **kw: run(argv))
    monkeypatch.setattr(gw, "pathlib_read", lambda p: "1\n")
    monkeypatch.setattr(gwguard, "uplink_interface", lambda: "awg0")
    monkeypatch.setattr(gwguard, "uplink_policy", lambda i: {"rule": True, "route": False})
    checks = {c.name: c for c in svc.plumbing_checks()}
    assert checks["политика аплинка"].ok is False and "маршрут в awg0" in checks["политика аплинка"].detail
    monkeypatch.setattr(gwguard, "uplink_policy", lambda i: {"rule": True, "route": True})
    checks = {c.name: c for c in svc.plumbing_checks()}
    assert checks["политика аплинка"].ok is True
    assert checks["маскарад в аплинк"].ok is True, "masquerade в awg0 стоит"
    # без маскарада в аплинк агент на чистой машине нем — проверка это видит
    run = _guard_run({"tunnel_nets4": ["10.9.1.0/24"], "tg_nets4": []}, masq=("end0",))
    monkeypatch.setattr(gw, "_run", run)
    monkeypatch.setattr(sp, "run", lambda argv, **kw: run(argv))
    checks = {c.name: c for c in svc.plumbing_checks()}
    assert checks["маскарад в аплинк"].ok is False and "перевыпусти конфигурацию" in checks["маскарад в аплинк"].detail
    monkeypatch.setattr(gwguard, "uplink_interface", lambda: "")
    assert {c.name: c for c in svc.plumbing_checks()}["политика аплинка"].ok is None


# ── экономия тика: аплинк и политика снимаются один раз, юнит — в статике ────

def test_tick_reuses_uplink_probe_for_heal(svc, monkeypatch):
    """Автодетект аплинка (три exec) и `ip -j rule/route` (два) шли в тике
    дважды: в проверках и в самовосстановлении. Heal берёт то, что только что
    сняли проверки; вне тика — пробует сам."""
    import subprocess as sp
    from awgbot.infra import gwguard
    monkeypatch.setattr(config, "GW_CLIENT_SUBNET", "10.9.1.0/24")
    run = _guard_run({"tunnel_nets4": ["10.9.1.0/24"], "tg_nets4": []})
    monkeypatch.setattr(gw, "_run", run)
    monkeypatch.setattr(sp, "run", lambda argv, **kw: run(argv))
    monkeypatch.setattr(gw, "pathlib_read", lambda p: "1\n")
    calls = {"iface": 0, "policy": 0, "ensure": []}

    def iface():
        calls["iface"] += 1; return "awg0"

    def policy(i):
        calls["policy"] += 1; return {"rule": False, "route": True}

    def ensure(i, state=None):
        calls["ensure"].append((i, state)); return ["правило по метке"]
    monkeypatch.setattr(gwguard, "uplink_interface", iface)
    monkeypatch.setattr(gwguard, "uplink_policy", policy)
    monkeypatch.setattr(gwguard, "uplink_policy_ensure", ensure)

    svc.plumbing_checks()
    assert svc.uplink_policy_heal() == ["правило по метке"]
    assert calls["iface"] == 1 and calls["policy"] == 1, "heal снял аплинк/политику заново"
    assert calls["ensure"] == [("awg0", {"rule": False, "route": True})]
    svc.uplink_policy_heal()                     # вне тика — свои пробы
    assert calls["iface"] == 2 and calls["policy"] == 2


def test_unit_enabled_is_cached_until_invalidated(svc, monkeypatch):
    """`systemctl is-enabled` — не на каждый тик: включённость юнита меняется
    только руками; кнопки «Статус»/«Монитор здоровья» сбрасывают кэш."""
    import subprocess as sp
    from awgbot.infra import gwguard
    monkeypatch.setattr(config, "GW_CLIENT_SUBNET", "10.9.1.0/24")
    base = _guard_run({"tunnel_nets4": ["10.9.1.0/24"], "tg_nets4": []})
    seen = []

    def run(argv, timeout=10, **kw):
        if list(argv)[:2] == ["systemctl", "is-enabled"]:
            seen.append(1)
        return base(argv, timeout, **kw)
    monkeypatch.setattr(gw, "_run", run)
    monkeypatch.setattr(sp, "run", lambda argv, **kw: run(argv))
    monkeypatch.setattr(gw, "pathlib_read", lambda p: "1\n")
    monkeypatch.setattr(gwguard, "uplink_interface", lambda: "")
    svc.plumbing_checks(); svc.plumbing_checks()
    assert len(seen) == 1, "юнит спрашивали на каждый вызов"
    svc.invalidate_static()
    svc.plumbing_checks()
    assert len(seen) == 2


def test_monitor_tick_is_a_single_commit(svc, monkeypatch):
    """Снимок панели, месячный трафик и стрики — один коммит за тик, а не три
    fsync на флеш малины."""
    monkeypatch.setattr(svc, "status", lambda: _quiet_status(rx=100, tx=50))
    monkeypatch.setattr(svc, "uplink_policy_heal", lambda: [])
    monkeypatch.setattr(svc, "tg_mark_ensure", lambda missing=None: 0)
    before = svc.db.commits
    svc.monitor_tick()
    assert svc.db.commits - before == 1, "тик стоил больше одной транзакции"
    assert svc.cached_status(60) is not None and svc.cached_status(60).month_rx == 100


def test_egress_probe_targets_in_parallel(svc, monkeypatch):
    """Две цели — разом: первый ответ и есть результат, медленную не ждём;
    все молчат — None."""
    import time as _t

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def connect(addr, timeout=3.0):
        host, _ = addr
        if host == "slow":
            _t.sleep(0.3); raise OSError("timeout")
        return _Conn()
    monkeypatch.setattr(gw.socket, "create_connection", connect)
    monkeypatch.setattr(gw.settings, "get", lambda k, d=None: ["slow", "fast"] if k.endswith("egress_targets") else d)
    t0 = _t.monotonic()
    assert svc.egress_probe() is not None
    assert _t.monotonic() - t0 < 0.2, "ждали медленную цель"

    def all_dead(addr, timeout=3.0):
        raise OSError("down")
    monkeypatch.setattr(gw.socket, "create_connection", all_dead)
    assert svc.egress_probe() is None


def test_egress_alert_has_its_own_short_streak(svc, monkeypatch):
    """Лежащий домашний канал равносилен лежащему линку — алерт на втором тике,
    а не на пятом."""
    monkeypatch.setattr(svc, "uplink_policy_heal", lambda: [])
    monkeypatch.setattr(svc, "tg_mark_ensure", lambda missing=None: 0)
    monkeypatch.setattr(svc, "status", lambda: _quiet_status(egress_ms=None))
    first = svc.monitor_tick()
    assert not any("не выходит наружу" in n.text for n in first)
    second = svc.monitor_tick()
    assert any("не выходит наружу" in n.text for n in second)
