"""Панель агента v2.4.3: снимок, потребление за месяц, имя ВПС, метрики."""
from __future__ import annotations

import json

import pytest

from awgbot.bot import texts
from awgbot.domain import gateway as gw
from awgbot.domain.gateway import GatewayServices, GwStatus, GwCheck
from awgbot.infra.db import Database
from awgbot.runtime import hostmetrics


@pytest.fixture()
def svc(tmp_path):
    d = Database(tmp_path / "gw.db"); d.init_schema()
    return GatewayServices(d)


def test_month_traffic_accumulates_across_counter_resets(svc, monkeypatch):
    """Счётчики линка обнуляются каждым рестартом интерфейса; месячный итог
    должен копить дельты и переживать обнуление, а новый месяц — начинать с нуля."""
    from awgbot.util import timeutil
    import datetime as dt
    t = [dt.datetime(2026, 9, 5, 12, 0, tzinfo=timeutil.TZ)]
    monkeypatch.setattr(gw.timeutil, "now", lambda: t[0])
    assert svc._account_traffic(100, 10) == (100, 10)      # первый замер — весь счётчик
    assert svc._account_traffic(160, 25) == (160, 25)      # +60 / +15
    assert svc._account_traffic(20, 5) == (180, 30)        # счётчик обнулился: +20 / +5
    t[0] = dt.datetime(2026, 10, 1, 0, 5, tzinfo=timeutil.TZ)
    assert svc._account_traffic(30, 7) == (10, 2)          # новый месяц: только дельта


def test_snapshot_roundtrip_keeps_checks_and_metrics(svc, monkeypatch):
    st = GwStatus(link_up=True, handshake_age=3.0, rx=5, tx=6,
                  checks=[GwCheck("линк", True), GwCheck("ядра", None, "нечем")],
                  cpu=4.0, ram=51.0, ram_free_mb=999, disk=58.0, disk_free_gb=100.0,
                  smart="OK", uptime_seconds=90000, hostname="NASPi", server_name="awg-srv")
    monkeypatch.setattr(svc, "status", lambda: st)
    got = svc.snapshot()
    assert got.month_rx == 5 and got.month_tx == 6 and got.ts
    back = svc.cached_status(60)
    assert back is not None
    assert [c.name for c in back.checks] == ["линк", "ядра"] and back.checks[1].ok is None
    assert back.smart == "OK" and back.hostname == "NASPi" and back.server_name == "awg-srv"


def test_server_name_prefers_setting_then_bundle_then_default(svc, monkeypatch):
    from awgbot.core import settings
    monkeypatch.setattr(settings, "get", lambda key, default=None: default)
    assert svc.server_name() == "ВПС"
    svc.db.set_state(GatewayServices._SERVER_NAME_KEY, "awg-srv")
    assert svc.server_name() == "awg-srv"
    monkeypatch.setattr(settings, "get",
                        lambda key, default=None: "my-vps" if key == "app.gateway.server_name" else default)
    assert svc.server_name() == "my-vps"


def test_apply_bundle_remembers_server_name(svc, monkeypatch, tmp_path):
    """Бандл несёт SERVER_NAME с ВПС — агент запоминает его для «Линк до …»."""
    from awgbot.util import bundlecrypt as bc
    from awgbot.core import config
    priv = "cOJ+yJKfw9Yq9HLm2Dq5PZv2xU0a5s5D3q1t0m2Xn1A="
    conf = tmp_path / "awg0.conf"; conf.write_text(f"[Interface]\nPrivateKey = {priv}\n")
    monkeypatch.setattr(config, "GW_LINK_CONF", str(conf))
    plain = ("#!/bin/sh\nSERVER_NAME=\"awg-srv\"\n#__GW_SETUP_BELOW__\n"
             "__LINK_CONF_EOF__\n").encode()
    blob = bc.encrypt(plain, bc.read_privkey(conf.read_text()))
    monkeypatch.setattr(gw, "_run", lambda argv, timeout=10: type("P", (), {
        "returncode": 0, "stdout": b"ok", "stderr": b""})())
    import os, tempfile
    monkeypatch.setattr(tempfile, "mkstemp", lambda **kw: (
        os.open(str(tmp_path / "b.sh"), os.O_RDWR | os.O_CREAT), str(tmp_path / "b.sh")))
    ok, _ = svc.apply_bundle(blob)
    assert ok and svc.db.get_state(GatewayServices._SERVER_NAME_KEY) == "awg-srv"


def test_panel_text_mirrors_the_main_bot_layout():
    st = GwStatus(link_up=True, handshake_age=69.0, cpu=4.0, temp=59.0, ram=51.0,
                  ram_free_mb=999, disk=58.0, disk_free_gb=100.0, smart="OK",
                  throttled={"raw": 0, "now": [], "ever": []}, uptime_seconds=17 * 86400 + 20 * 3600,
                  hostname="NASPi", server_name="awg-srv",
                  checks=[GwCheck("линк", True)], month_rx=10 * 1024 ** 3, month_tx=175 * 1024 ** 3)
    out = texts.gateway_panel(st)
    for needle in ("🛰 <b>РФ-шлюз (NASPi)</b>", "🖥 Сервер: 🟢 работает", "⬆️ Аптайм: 17 дней 20 часов",
                   "📡 Линк до awg-srv: 🟢 хендшейк 69 с назад", "📈 CPU: 4%, 59°C",
                   "RAM: 51%, свободно 999 МБ", "Диск: 58%, свободно 100 ГБ, SMART: OK",
                   "Питание: ОК", "Обновлено только что", "🌡 Монитор здоровья: ✅ проблем не выявлено",
                   "📊 Потребление за месяц: 185.0 ГБ (↑ 10.0 ГБ | ↓ 175.0 ГБ)"):
        assert needle in out, needle
    assert "Внешний IP" not in out and "Модуль awg" not in out and "Ядра" not in out


def test_panel_health_line_counts_problems():
    st = GwStatus(checks=[GwCheck("MASQUERADE", False, "нет"), GwCheck("линк", False, "лежит"),
                          GwCheck("ядра", True)])
    assert "🔴 проблем: 2 — MASQUERADE, линк" in texts.gateway_panel(st)


def test_health_screen_carries_module_and_kernels():
    st = GwStatus(checks=[GwCheck("ядра", True)], module_version="1.0.2026", srcversion="ABCDEF1234",
                  kernels_total=1, throttled={"raw": 0, "now": [], "ever": ["недонапряжение случалось"]})
    out = texts.gateway_health(st)
    assert "Модуль awg: 1.0.2026, srcversion ABCDEF12…; загружаемых ядер: 1" in out
    assert "Питание: ОК (с загрузки: недонапряжение случалось)" in out
    assert "Проблем не выявлено." in out


def test_root_block_device_strips_partition(tmp_path):
    for src, want in (("/dev/sda2", "/dev/sda"), ("/dev/mmcblk0p2", "/dev/mmcblk0"),
                      ("/dev/nvme0n1p3", "/dev/nvme0n1")):
        m = tmp_path / "mounts"; m.write_text(f"proc /proc proc rw 0 0\n{src} / ext4 rw 0 0\n")
        assert hostmetrics.root_block_device(str(m)) == want


def test_smart_is_not_asked_on_sd_cards(monkeypatch):
    monkeypatch.setattr(hostmetrics, "root_block_device", lambda: "/dev/mmcblk0")
    assert hostmetrics.read_smart_health() is None
