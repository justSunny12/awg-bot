"""Резолвер админа («SMB в туннеле») при устойчивом отказе записей: журнал
— один раз на текст отказа, отметка «молчал прошлый тик» — записью только
при смене значения.

Цена ошибки: одна и та же строка отказа в журнале каждую минуту (и
перезапуск экземпляра резолвера каждым тактом — пауза проверена в
test_smb_tunnel); запись в базу на каждом тике монитора — износ карты
памяти и шум WAL.
"""
from __future__ import annotations

import logging

import pytest

from awgbot.infra import admindns
from tests.integration import test_smb_tunnel as _t
from tests.integration.test_smb_tunnel import NAS, _seen

pytestmark = pytest.mark.integration
adm, host, toggle, two_slots = _t.adm, _t.host, _t.toggle, _t.two_slots     # фикстуры сцены

PAUSE = "awgbot.domain.services.gwchannel.smbtunnel._FAIL_PAUSE_S"
FAIL_KEY = "admin_dns_failed_once"


def test_a_steady_refusal_is_logged_once_per_text(services, two_slots, adm, toggle, monkeypatch, caplog):
    services.gwlink_services_in(1, [NAS]); _seen(services, 1, 1)
    toggle["on"] = True
    monkeypatch.setattr(PAUSE, 0)                         # повторы — каждый такт, журнал — нет
    why = {"text": "dnsmasq: нет места на диске"}

    def run(mode, arg=""):
        if mode == "records":
            raise admindns.AdminDnsError(why["text"])
        return ""
    monkeypatch.setattr(admindns, "run", run)
    caplog.set_level(logging.WARNING)

    def logged() -> list[str]:
        return [r.getMessage() for r in caplog.records if "записи не поставлены" in r.getMessage()]
    for _ in range(4):
        assert services.smb_tunnel_sync() is False
    assert logged() == ["SMB в туннеле: записи не поставлены: dnsmasq: нет места на диске"], logged()
    why["text"] = "dnsmasq: синтаксис"
    services.smb_tunnel_sync()
    services.smb_tunnel_sync()
    assert len(logged()) == 2 and logged()[-1].endswith("dnsmasq: синтаксис"), "новый отказ не попал в журнал"


def test_the_silence_mark_is_written_only_when_it_changes(services, two_slots, adm, monkeypatch):
    writes: list[str] = []
    real = services.db.set_state

    def counted(key, value):
        if key == FAIL_KEY:
            writes.append(value)
        return real(key, value)
    monkeypatch.setattr(services.db, "set_state", counted)
    for _ in range(5):
        services.admin_dns_health_tick()
    assert writes == [], f"резолвер отвечает, а отметка пишется каждым тиком: {writes}"
    adm.alive = False
    services.admin_dns_health_tick()
    assert writes == ["1"]
    adm.alive = True
    for _ in range(5):
        services.admin_dns_health_tick()
    assert writes == ["1", "0"], writes
