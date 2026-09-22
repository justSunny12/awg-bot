"""Детект перезагрузки хоста, проба наружу в статусе шлюза, джиттер сетевых задач."""
from __future__ import annotations

import pytest

from awgbot.domain import gateway as gw
from awgbot.domain.gateway import GatewayServices
from awgbot.infra.db import Database
from awgbot.runtime import hostboot


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "a.db"); d.init_schema()
    return d


def test_reboot_detected_only_on_boot_id_change(db):
    assert hostboot.reboot_detected(db, boot_id="aaa") is False      # первый запуск
    assert hostboot.reboot_detected(db, boot_id="aaa") is False      # рестарт сервиса
    assert hostboot.reboot_detected(db, boot_id="bbb") is True       # ребут
    assert hostboot.reboot_detected(db, boot_id="bbb") is False
    assert hostboot.reboot_detected(db, boot_id="") is False


def test_host_rebooted_text():
    from awgbot.bot import texts
    assert texts.host_rebooted("NASPi", "агента") == "⚠️ Хост NASPi был перезагружен.\n✅ Запуск агента успешен"


def test_egress_check_in_snapshot(db, monkeypatch):
    svc = GatewayServices(db)
    # Зонд наружу теперь идёт не каждым тиком: в простое такт растягивается, и
    # второй снимок подряд жил бы кэшем вердикта. Здесь проверяется сама
    # проверка в снимке, поэтому растяжку снимаем штатным рычагом — множителем 0.
    real = gw.settings.get
    monkeypatch.setattr(gw.settings, "get", lambda k, d=None:
                        0 if k == "app.gateway.egress_idle_multiplier" else real(k, d))
    monkeypatch.setattr(svc, "link_status", lambda: (True, 5.0, 0, 0))
    monkeypatch.setattr(svc, "plumbing_checks", lambda: [])
    monkeypatch.setattr(svc, "tg_mark_missing", lambda info=None: [])
    monkeypatch.setattr(svc, "versions", lambda: ("v", "s"))
    monkeypatch.setattr(svc, "kernel_coverage", lambda: ([], 1))
    monkeypatch.setattr(svc, "egress_probe", lambda: 42.0)
    st = svc.snapshot()
    egress = [c for c in st.checks if c.name == "выход наружу"][0]
    assert egress.ok is True and "42 мс" in egress.detail
    monkeypatch.setattr(svc, "egress_probe", lambda: None)
    st = svc.snapshot()
    assert [c for c in st.checks if c.name == "выход наружу"][0].ok is False


def test_network_jobs_have_jitter():
    from awgbot.runtime import scheduler as sched
    trig = sched.update_check_trigger()
    assert trig is not None and trig.jitter == 1800
