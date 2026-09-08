"""Доступность за сутки (часовые корзины), детект перезагрузки хоста, проба
наружу в статусе шлюза, джиттер сетевых задач."""
from __future__ import annotations

import datetime as dt

import pytest

from awgbot.domain.gateway import GatewayServices, GwStatus
from awgbot.infra.db import Database
from awgbot.runtime import hostboot
from awgbot.util import availability, timeutil


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "a.db"); d.init_schema()
    return d


def test_availability_percent_over_last_day_only(db):
    now = timeutil.now().replace(minute=30)
    for i in range(30):                         # 30 часов назад … сейчас, по одному замеру
        t = now - dt.timedelta(hours=29 - i)
        availability.record(db, "k", ok=(i % 2 == 0), now=t)
    pct = availability.percent(db, "k", now=now)
    assert pct is not None and 40 <= pct <= 60
    assert availability.percent(db, "nothing", now=now) is None


def test_reboot_detected_only_on_boot_id_change(db):
    assert hostboot.reboot_detected(db, boot_id="aaa") is False      # первый запуск
    assert hostboot.reboot_detected(db, boot_id="aaa") is False      # рестарт сервиса
    assert hostboot.reboot_detected(db, boot_id="bbb") is True       # ребут
    assert hostboot.reboot_detected(db, boot_id="bbb") is False
    assert hostboot.reboot_detected(db, boot_id="") is False


def test_host_rebooted_text():
    from awgbot.bot import texts
    assert texts.host_rebooted("NASPi", "агента") == "⚠️ Хост NASPi был перезагружен.\n✅ Запуск агента успешен"


def test_egress_check_and_link_availability_in_snapshot(db, monkeypatch):
    svc = GatewayServices(db)
    from awgbot.domain import gateway as gw
    monkeypatch.setattr(svc, "link_status", lambda: (True, 5.0, 0, 0))
    monkeypatch.setattr(svc, "plumbing_checks", lambda: [])
    monkeypatch.setattr(svc, "tg_mark_missing", lambda: [])
    monkeypatch.setattr(svc, "versions", lambda: ("v", "s"))
    monkeypatch.setattr(svc, "kernel_coverage", lambda: ([], 1))
    monkeypatch.setattr(svc, "egress_probe", lambda: 42.0)
    st = svc.snapshot()
    egress = [c for c in st.checks if c.name == "выход наружу"][0]
    assert egress.ok is True and "42 мс" in egress.detail
    assert st.link_avail == 100.0
    monkeypatch.setattr(svc, "egress_probe", lambda: None)
    monkeypatch.setattr(svc, "link_status", lambda: (False, None, 0, 0))
    st = svc.snapshot()
    assert [c for c in st.checks if c.name == "выход наружу"][0].ok is False
    assert st.link_avail == 50.0
    assert "Доступность линка за сутки: 50%" in __import__("awgbot.bot.texts", fromlist=["x"]).gateway_panel(st)


def test_network_jobs_have_jitter():
    from awgbot.runtime import scheduler as sched
    trig = sched.update_check_trigger()
    assert trig is not None and trig.jitter == 1800
