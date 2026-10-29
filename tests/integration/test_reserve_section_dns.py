"""Раздел «🛟 Резерв» без пары: данные записи DNS в `reserve_section_data()["dns"]`
(domain/services/reserve/screen.py) и строка записи в тексте раздела
(bot/texts/reserve/section.section_text) — по настройке «🌐 DNS-запись».

Регрессия: строка записи была зашита текстом «🔴 DNS-запись: не настроена …»
и показывалась всегда — даже при настроенном провайдере с проверенной
записью. Провайдер и авторитетный сервер подставлены (`DnsWorld` из
`_reserve_move`).

Цена ошибки: админ с рабочим DigitalPlat и зелёной проверкой видит красную
строку, думает, что аварийный переезд не сработает, и перенастраивает то,
что работает; при адресе-IP строка о записи сбивает с толку — записи у IP
нет, об этом говорит отдельная строка ⛔.
"""
from __future__ import annotations

import re

import pytest

from awgbot.bot.texts.reserve import section as st
from awgbot.core import settings

from tests.integration import _reserve_common as rc
from tests.integration import _reserve_move as rm

pytestmark = pytest.mark.integration
rsv = rc.rsv
mv = rm.mv

KEY = "dp_live_DUMMY"


def _checked_digitalplat(svc) -> None:
    svc.reserve_dns_set_provider("digitalplat")
    svc.reserve_dns_set_token(KEY)
    assert svc.reserve_dns_check()["ok"] is True, "подставной мир: проверка должна проходить"


def _record_lines(text: str) -> list[str]:
    return [ln for ln in text.split("\n") if "DNS-запись:" in ln]


# ── данные экрана ───────────────────────────────────────────────────────────

def test_section_data_carries_the_checked_provider_with_its_token_and_check_result(mv):
    """Провайдер с токеном и прошедшей проверкой — экран получает именно это, а не пустую заготовку."""
    _checked_digitalplat(mv.svc)
    dns = mv.svc.reserve_section_data()["dns"]
    assert dns["provider"] == "digitalplat" and dns["provider_label"] == "DigitalPlat"
    assert dns["addrs"] and dns["ttl"] >= 0 and dns["ttl_warn"] == 300, "адреса и TTL проверки — строке нужны для ⚠️"
    assert dns["has_token"] is True and dns["check_ok"] is True and dns["check_why"] == ""
    assert dns["checked_at"], "время проверки есть в состоянии — экран без него не покажет «проверено в»"
    assert KEY not in str(dns), "сам токен на экран раздела не нужен"


def test_section_data_without_a_provider_is_manual_without_a_token(mv):
    """Ничего не настроено — «вручную» без токена и без итога проверки: красная строка про переезд кнопкой."""
    dns = mv.svc.reserve_section_data()["dns"]
    assert dns == {"provider": "manual", "provider_label": "вручную", "has_token": False, "check_ok": None,
                   "checked_at": "", "check_why": "", "addrs": [], "ttl": 0, "ttl_warn": 300}


def test_changing_the_provider_drops_the_check_result_in_section_data(mv):
    """Смена провайдера — прежняя проверка не про него: раздел обязан снова показать «не проверена»/«токен не задан»."""
    _checked_digitalplat(mv.svc)
    mv.svc.reserve_dns_set_provider("cloudflare")
    dns = mv.svc.reserve_section_data()["dns"]
    assert dns["provider"] == "cloudflare" and dns["provider_label"] == "Cloudflare"
    assert dns["check_ok"] is None and dns["checked_at"] == "", "итог проверки прежнего провайдера показан как свежий"
    assert dns["has_token"] is False, "токен прежнего провайдера не годится новому"


def test_a_failed_check_reaches_section_data_with_its_reason(mv):
    """Провайдер отказал — причина доезжает до раздела: админ видит, почему переезд будет только кнопкой."""
    mv.svc.reserve_dns_set_provider("digitalplat")
    mv.svc.reserve_dns_set_token(KEY)
    mv.dns.check_error = "ключ без права dns.write"
    assert mv.svc.reserve_dns_check()["ok"] is False
    dns = mv.svc.reserve_section_data()["dns"]
    assert dns["check_ok"] is False and dns["check_why"] == "ключ без права dns.write"


# ── текст раздела ───────────────────────────────────────────────────────────

def test_a_checked_provider_shows_a_green_record_line_and_no_red_not_configured(mv):
    """Регрессия: DigitalPlat с токеном и прошедшей проверкой — зелёная строка со временем, красной «не настроена» нет."""
    _checked_digitalplat(mv.svc)
    text = st.section_text(mv.svc.reserve_section_data())
    assert "не настроена" not in text, f"зашитая строка «не настроена» вернулась:\n{text}"
    lines = _record_lines(text)
    assert len(lines) == 1, f"строка записи ровно одна:\n{text}"
    assert lines[0].startswith("🟢 DNS-запись: DigitalPlat — бот меняет её сам · проверено "), lines[0]
    assert re.search(r"проверено \d\d\.\d\d \d\d:\d\d \(UTC", lines[0])


def test_an_unconfigured_record_is_a_red_manual_line_after_the_address(mv):
    """Ничего не настроено — красная строка «вручную» сразу после адреса и порта, а не выдуманная «не настроена»."""
    text = st.section_text(mv.svc.reserve_section_data())
    lines = text.split("\n")
    i = next(i for i, ln in enumerate(lines) if ln.startswith("🟢 Доменное имя сервера: "))
    assert rc.HOST_A in lines[i]
    rec = _record_lines(text)
    assert rec == ["🔴 DNS-запись: вручную — переезд только кнопкой, запись меняешь у провайдера сам"]
    assert lines.index(rec[0]) in (i + 1, i + 2), "строка записи — рядом с адресом, под строкой порта"
    assert "не настроена" not in text


def test_a_failed_check_is_a_red_line_with_the_reason_in_the_section(mv):
    """Отказ провайдера виден в разделе с причиной — не зелёная и не «не проверена»."""
    mv.svc.reserve_dns_set_provider("digitalplat")
    mv.svc.reserve_dns_set_token(KEY)
    mv.dns.check_error = "ключ без права dns.write"
    mv.svc.reserve_dns_check()
    rec = _record_lines(st.section_text(mv.svc.reserve_section_data()))
    assert rec == ["🔴 DNS-запись: DigitalPlat — проверка не прошла: ключ без права dns.write"]


def test_an_ip_address_has_no_record_line_at_all(mv):
    """Адрес для пользователей — IP: записи DNS у него нет, строка про неё не показывается ни в каком состоянии."""
    _checked_digitalplat(mv.svc)
    settings.set_value("app.network.server_host", "203.0.113.5")
    d = mv.svc.reserve_section_data()
    assert d["host_is_ip"] is True
    text = st.section_text(d)
    assert _record_lines(text) == [], f"строка записи при IP:\n{text}"
    assert "⛔ Вместо доменного имени сервера — IP <code>203.0.113.5</code>" in text


def test_no_address_has_no_record_line_either(mv):
    """Адрес не задан — строки записи нет: сначала домен в «🖥 Сервер AWG», потом запись."""
    settings.set_value("app.network.server_host", "")
    text = st.section_text(mv.svc.reserve_section_data())
    assert _record_lines(text) == []
    assert "⛔ Доменное имя сервера не задано" in text
