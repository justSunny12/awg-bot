"""Агент шлюза: снимок, потребление за месяц, имя ВПС, метрики, бандл и
бэкап. Панель, здоровье и вопрос о файле конфигурации — в эталонах экранов
(tests/screens/gateway.txt); здесь — данные под ними."""
from __future__ import annotations


import pytest

from awgbot.domain import gateway as gw
from awgbot.domain.gateway import GatewayServices, GwStatus, GwCheck
from awgbot.infra.db import Database
from awgbot.infra import hostmetrics


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
    monkeypatch.setattr(gw.base.timeutil, "now", lambda: t[0])
    assert svc._account_traffic(100, 10) == (100, 10)      # первый замер — весь счётчик
    assert svc._account_traffic(160, 25) == (160, 25)      # +60 / +15
    assert svc._account_traffic(20, 5) == (180, 30)        # счётчик обнулился: +20 / +5
    t[0] = dt.datetime(2026, 10, 1, 0, 5, tzinfo=timeutil.TZ)
    assert svc._account_traffic(30, 7) == (10, 2)          # новый месяц: только дельта
    # пояс сменился на западный: «сейчас» снова сентябрь — октябрьский итог не обнуляется
    t[0] = dt.datetime(2026, 9, 30, 23, 30, tzinfo=timeutil.TZ)
    assert svc._account_traffic(40, 9) == (20, 4)


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
    assert svc.server_name() == "сервера AWG"
    svc.db.set_state(GatewayServices._SERVER_NAME_KEY, "awg-srv")
    assert svc.server_name() == "awg-srv"
    monkeypatch.setattr(settings, "get",
                        lambda key, default=None: "my-vps" if key == "app.gateway.server_name" else default)
    assert svc.server_name() == "my-vps"


def test_root_block_device_strips_partition(tmp_path):
    for src, want in (("/dev/sda2", "/dev/sda"), ("/dev/mmcblk0p2", "/dev/mmcblk0"),
                      ("/dev/nvme0n1p3", "/dev/nvme0n1")):
        m = tmp_path / "mounts"; m.write_text(f"proc /proc proc rw 0 0\n{src} / ext4 rw 0 0\n")
        assert hostmetrics.root_block_device(str(m)) == want


def test_smart_is_not_asked_on_sd_cards(monkeypatch):
    monkeypatch.setattr(hostmetrics, "root_block_device", lambda: "/dev/mmcblk0")
    assert hostmetrics.read_smart_health() is None
