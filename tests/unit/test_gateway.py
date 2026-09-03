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
        if "latest-handshakes" in argv:
            return _cp(0, f"PUB1\t{int(now-40)}\n")
        if "transfer" in argv:
            return _cp(0, f"PUB1\t1000\t2000\n")
        return _cp(1)

    monkeypatch.setattr(gw, "_run", run)
    up, age, rx, tx = svc.link_status()
    assert up and 35 <= age <= 60 and rx == 1000 and tx == 2000


def test_plumbing_isolation_requires_drop_before_accept(svc, monkeypatch):
    """DROP-правила изоляции обязаны стоять выше ACCEPT: обратный порядок
    открывает клиентам домашнюю сеть, оставаясь внешне «настроенной цепочкой»."""
    def run(argv, timeout=10):
        if argv[:2] == ["iptables", "-S"]:
            return _cp(0, "-N AWGLINK_FWD\n-A AWGLINK_FWD -j ACCEPT\n"
                          "-A AWGLINK_FWD -d 10.0.0.0/8 -j DROP\n")
        if argv[:2] == ["iptables", "-C"]:
            return _cp(0)
        if argv[:2] == ["systemctl", "is-enabled"]:
            return _cp(0)
        if argv[:3] == ["ip", "route", "show"]:
            return _cp(0, "default via 1.2.3.4 dev eth0\n")
        return _cp(0)

    monkeypatch.setattr(gw, "_run", run)
    monkeypatch.setattr(gw, "pathlib_read", lambda p: "1\n")
    checks = {c.name: c for c in svc.plumbing_checks()}
    assert checks["изоляция LAN"].ok is False
    assert checks["ip_forward"].ok is True


def test_masquerade_check_disabled_without_subnet_is_unknown_not_ok(svc, monkeypatch):
    """Без gateway.client_subnet проверка MASQUERADE выключена — и это ⚪
    «нечем проверить», а не ✅: спутать их значит прозевать пропажу NAT."""
    monkeypatch.setattr(config, "GW_CLIENT_SUBNET", "")
    monkeypatch.setattr(gw, "_run", lambda a, timeout=10: _cp(0, "default dev eth0\n"))
    monkeypatch.setattr(gw, "pathlib_read", lambda p: "1\n")
    checks = {c.name: c for c in svc.plumbing_checks()}
    assert checks["MASQUERADE"].ok is None


# ── тик монитора ─────────────────────────────────────────────────────────────

def _quiet_status(**kw):
    st = GwStatus(link_up=True, handshake_age=10.0,
                  checks=[GwCheck("MASQUERADE", True)], temp=50.0, disk=30.0,
                  throttled={"raw": 0, "now": [], "ever": []},
                  module_version="v", srcversion="s", kernels_total=3)
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def test_external_ip_change_alerts_loudly_but_first_sighting_is_silent(
        svc, monkeypatch):
    """Смена внешнего IP — громкий алерт (эндпоинт линка на ВПС смотрит на
    DDNS), но ПЕРВОЕ знакомство с адресом — не смена, алерта нет."""
    monkeypatch.setattr(svc, "status", lambda: _quiet_status())
    monkeypatch.setattr(svc, "fetch_external_ip", lambda: "1.1.1.1")
    assert [n for n in svc.monitor_tick() if "IP" in n.text] == []
    monkeypatch.setattr(svc, "fetch_external_ip", lambda: "2.2.2.2")
    notes = [n for n in svc.monitor_tick() if "IP" in n.text]
    assert len(notes) == 1 and notes[0].force_sound is True
    assert "1.1.1.1" in notes[0].text and "2.2.2.2" in notes[0].text
    assert [n for n in svc.monitor_tick() if "IP" in n.text] == [], \
        "тот же адрес алертит повторно"


def test_dead_link_alerts_after_two_ticks(svc, monkeypatch):
    monkeypatch.setattr(svc, "fetch_external_ip", lambda: "")
    monkeypatch.setattr(svc, "status", lambda: _quiet_status(handshake_age=9999.0))
    assert svc.monitor_tick() == []
    notes = svc.monitor_tick()
    assert len(notes) == 1 and "Линк" in notes[0].text and notes[0].force_sound


def test_quiet_gateway_produces_no_notes_and_stores_snapshot(svc, monkeypatch):
    monkeypatch.setattr(svc, "fetch_external_ip", lambda: "")
    monkeypatch.setattr(svc, "status", lambda: _quiet_status())
    assert svc.monitor_tick() == []
    import json
    snap = json.loads(svc.db.get_state("gw_status"))
    assert snap["link_up"] is True and snap["handshake_age"] == 10.0


# ── панель ───────────────────────────────────────────────────────────────────

def test_panel_renders_on_a_dead_gateway():
    """Панель обязана рисоваться и на полумёртвом шлюзе — именно тогда она
    нужнее всего."""
    from awgbot.bot import texts
    out = texts.gateway_panel(GwStatus())
    assert "лежит" in out


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


def test_tg_mark_ensure_adds_only_missing(svc, monkeypatch):
    present = {"149.154.160.0/20"}
    added = []

    def run(argv, timeout=10):
        if "-C" in argv:
            return _cp(0) if argv[argv.index("-d") + 1] in present else _cp(1)
        if "-A" in argv:
            added.append(argv[argv.index("-d") + 1]); return _cp(0)
        return _cp(0)

    monkeypatch.setattr(gw, "_run", run)
    assert svc.tg_mark_ensure() == len(svc.TG_RANGES) - 1
    assert "149.154.160.0/20" not in added


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


def test_status_caches_static_probes(svc, monkeypatch):
    """modinfo и обход /lib/modules — по кэшу с TTL: два подряд status() дают
    один modinfo; по истечении TTL — снова живьём."""
    calls = []

    def fake_run(argv, timeout=10):
        calls.append(argv[0])
        if argv[0] == "modinfo":
            return _cp(0, "version: 1.0\nsrcversion: ABC\n")
        return _cp(1, "")

    monkeypatch.setattr(gw, "_run", fake_run)
    monkeypatch.setattr(gw.hostmetrics if hasattr(gw, "hostmetrics") else
                        __import__("awgbot.runtime.hostmetrics", fromlist=["x"]),
                        "read_pi_throttled", lambda: None)
    clock = [1000.0]
    monkeypatch.setattr(gw.time, "monotonic", lambda: clock[0])
    st1 = svc.status(); st2 = svc.status()
    assert st1.srcversion == st2.srcversion == "ABC"
    assert calls.count("modinfo") == 1
    clock[0] += GatewayServices._STATIC_TTL_SECONDS + 1
    svc.status()
    assert calls.count("modinfo") == 2
