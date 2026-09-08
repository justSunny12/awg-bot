"""Истекающие подписки: правило порога по длине периода; месяцу не показывать
порог «30 дней»."""
from __future__ import annotations

import datetime as dt

import pytest

import awgbot.core.config as cfg
from awgbot.domain.services import Services
from awgbot.util import timeutil


def test_expiring_limit_days_rule():
    assert Services.expiring_limit_days(365) == 30      # год: ⌊365/12⌋
    assert Services.expiring_limit_days(31) == 7        # месяц: max(2, 7)
    assert Services.expiring_limit_days(12) == 7
    assert Services.expiring_limit_days(7) is None      # неделя — всегда в списке
    assert Services.expiring_limit_days(1) is None


def _set_period(services, client, start: dt.datetime, end: dt.datetime, kind: str):
    services.db.update_client_fields(client.id, period_start=timeutil.to_iso(start),
                                     period_end=timeutil.to_iso(end), period_kind=kind)


def test_expiring_subscriptions_by_rule(services, make_active_client):
    now = timeutil.now()
    year_far = make_active_client("Год далеко", tg_id=2001)
    _set_period(services, year_far, now - dt.timedelta(days=300), now + dt.timedelta(days=65), "year")
    year_soon = make_active_client("Год скоро", tg_id=2002)
    _set_period(services, year_soon, now - dt.timedelta(days=340), now + dt.timedelta(days=25), "year")
    month_far = make_active_client("Месяц далеко", tg_id=2003)
    _set_period(services, month_far, now - dt.timedelta(days=10), now + dt.timedelta(days=21), "month")
    month_soon = make_active_client("Месяц скоро", tg_id=2004)
    _set_period(services, month_soon, now - dt.timedelta(days=25), now + dt.timedelta(days=6), "month")
    week = make_active_client("Неделя", tg_id=2005)
    _set_period(services, week, now, now + dt.timedelta(days=7), "week")
    names = [c.name for c, _ in services.expiring_subscriptions()]
    assert names == ["Месяц скоро", "Неделя", "Год скоро"], names   # по остатку, ближайшие сверху


def test_month_never_gets_the_30_day_threshold(services, make_active_client, monkeypatch):
    monkeypatch.setattr(cfg, "NOTIFY_THRESHOLDS_MINUTES", [(43200, "30 дней"), (10080, "7 дней")])
    now = timeutil.now()
    c = make_active_client("Месяц 31", tg_id=2010)
    _set_period(services, c, now, now + dt.timedelta(days=31), "month")   # 31 день > 30
    services.db.update_client_fields(c.id, status="active")
    notes = services.check_expiry()
    assert not any("30 дней" in n.text for n in notes)
    y = make_active_client("Год", tg_id=2011)
    _set_period(services, y, now - dt.timedelta(days=340), now + dt.timedelta(days=25), "year")
    notes = services.check_expiry()
    assert any("30 дней" in n.text for n in notes)
