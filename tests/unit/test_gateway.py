"""Роль gateway: пробы, гистерезис, панель (docs/ROADMAP.md, п.7, этап 1)."""
from __future__ import annotations

import subprocess
import types

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


def _guard_json(sets: dict, chains=None):
    chains = _ALL_CHAINS if chains is None else chains
    import json
    items = [{"metainfo": {}}]
    for name, elems in sets.items():
        items.append({"set": {"family": "inet", "name": name, "table": "awg_gw_guard",
                              "elem": list(elems)}})
    for c in chains:
        items.append({"chain": {"family": "inet", "table": "awg_gw_guard", "name": c}})
    return json.dumps({"nftables": items})


def _guard_run(sets, chains=None, fwd_policy="accept"):
    """_run/subprocess-стаб: таблица awg_gw_guard в JSON, чужой FORWARD с политикой."""
    import json

    def run(argv, timeout=10, **kw):
        a = list(argv)
        if a[:1] == ["nft"]:
            a = a[1:]
        if a[:3] == ["-j", "list", "table"]:
            return _cp(0, _guard_json(sets, chains))
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
                  module_version="v", srcversion="s", kernels_total=3)
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
