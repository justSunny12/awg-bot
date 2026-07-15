"""Unit: awgbot.util.timeutil — время UTC+3, периоды, форматтеры (чистая логика)."""
from datetime import datetime, timezone

import pytest

from awgbot.core import config
from awgbot.core import settings
from awgbot.util import timeutil as t

pytestmark = pytest.mark.unit
TZ = t.TZ


def _dt(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ)


# ── ISO ──────────────────────────────────────────────────────────────────────
def test_iso_roundtrip():
    d = _dt(2026, 7, 5, 19, 8, 2)
    assert t.parse_iso(t.to_iso(d)) == d


def test_parse_iso_naive_treated_as_utc3():
    assert t.parse_iso("2026-07-05T19:08:02") == _dt(2026, 7, 5, 19, 8, 2)


def test_to_iso_converts_from_other_tz():
    utc = datetime(2026, 7, 5, 16, 8, 2, tzinfo=timezone.utc)   # = 19:08 UTC+3
    assert t.parse_iso(t.to_iso(utc)) == _dt(2026, 7, 5, 19, 8, 2)


# ── периоды ──────────────────────────────────────────────────────────────────
def test_add_period_day_week():
    start = _dt(2026, 3, 10, 12)
    assert t.add_period(start, "day") == _dt(2026, 3, 11, 12)
    assert t.add_period(start, "week") == _dt(2026, 3, 17, 12)


def test_add_period_month_is_calendar():
    # 31 января + месяц → 28 февраля (relativedelta, не 30 дней)
    assert t.add_period(_dt(2026, 1, 31), "month") == _dt(2026, 2, 28)


def test_add_period_year():
    assert t.add_period(_dt(2026, 1, 31), "year") == _dt(2027, 1, 31)


def test_add_period_extra_seconds():
    start = _dt(2026, 3, 10, 12)
    assert t.add_period(start, "day", extra_seconds=3600) == _dt(2026, 3, 11, 13)


def test_add_period_unknown_raises():
    with pytest.raises(ValueError):
        t.add_period(_dt(2026, 1, 1), "fortnight")


def test_remaining_seconds_sign():
    end = _dt(2026, 5, 1, 12)
    assert t.remaining_seconds(end, ref=_dt(2026, 5, 1, 11)) == 3600
    assert t.remaining_seconds(end, ref=_dt(2026, 5, 1, 13)) == -3600


# ── ceil_days ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("secs,days", [(0, 0), (1, 1), (86400, 1), (86401, 2), (172800, 2)])
def test_ceil_days(secs, days):
    assert t.ceil_days(secs) == days


# ── тихие часы (окно может пересекать полночь) ───────────────────────────────
def test_quiet_hours_same_day():
    assert t.in_quiet_hours(8, 20, _dt(2026, 1, 1, 10)) is True
    assert t.in_quiet_hours(8, 20, _dt(2026, 1, 1, 20)) is False   # верхняя граница строгая
    assert t.in_quiet_hours(8, 20, _dt(2026, 1, 1, 7)) is False


def test_quiet_hours_over_midnight():
    assert t.in_quiet_hours(20, 7, _dt(2026, 1, 1, 23)) is True
    assert t.in_quiet_hours(20, 7, _dt(2026, 1, 1, 3)) is True
    assert t.in_quiet_hours(20, 7, _dt(2026, 1, 1, 20)) is True     # нижняя граница включена
    assert t.in_quiet_hours(20, 7, _dt(2026, 1, 1, 7)) is False
    assert t.in_quiet_hours(20, 7, _dt(2026, 1, 1, 12)) is False


def test_quiet_hours_empty_window():
    assert t.in_quiet_hours(5, 5, _dt(2026, 1, 1, 5)) is False


# ── handshake онлайн-детект ──────────────────────────────────────────────────
def test_handshake_online():
    ref = _dt(2026, 6, 1, 12)
    base = int(ref.timestamp())
    assert t.handshake_is_online(None, ref=ref) is False
    assert t.handshake_is_online(base - 10, ref=ref) is True
    assert t.handshake_is_online(base - (settings.get_int("app.online_handshake_seconds", 300) + 100), ref=ref) is False
    assert t.handshake_is_online(base + 100, ref=ref) is False       # из будущего → не онлайн


# ── склонения через fmt_remaining_short ──────────────────────────────────────
@pytest.mark.parametrize("days,word", [
    (1, "день"), (2, "дня"), (5, "дней"), (11, "дней"), (21, "день"), (22, "дня"),
])
def test_plural_days(days, word):
    assert t.fmt_remaining_short(days * 86400) == f"{days} {word}"


def test_fmt_remaining_short_components():
    assert t.fmt_remaining_short(0) == "0 минут"
    assert t.fmt_remaining_short(86400 + 3600) == "1 день 1 час"
    assert t.fmt_remaining_short(2 * 86400 + 3 * 3600) == "2 дня 3 часа"
    assert t.fmt_remaining_short(3600) == "1 час"
    assert t.fmt_remaining_short(300) == "5 минут"          # <часа → минуты


def test_fmt_remaining_expired_and_small():
    past = _dt(2026, 1, 1, 12)
    assert t.fmt_remaining(past, ref=_dt(2026, 1, 1, 13)) == "истекло"
    soon = _dt(2026, 1, 1, 12, 0, 30)
    assert t.fmt_remaining(soon, ref=_dt(2026, 1, 1, 12)) == "меньше минуты"
    assert t.fmt_remaining(_dt(2026, 1, 6, 15), ref=_dt(2026, 1, 1, 12)) == "5 дней 3 часа"


# ── формат даты Amnezia (Qt::TextDate, день без паддинга) ────────────────────
def test_amnezia_date_format():
    assert t.amnezia_date(_dt(2026, 7, 5, 19, 8, 2)) == "Sun Jul 5 19:08:02 2026"


# ── docker StartedAt ─────────────────────────────────────────────────────────
def test_parse_docker_time_valid_and_tz():
    got = t.parse_docker_time("2026-07-05T10:39:22.158372221Z")   # 10:39 UTC = 13:39 UTC+3
    assert got is not None and got.hour == 13 and got.minute == 39


def test_parse_docker_time_never_and_garbage():
    assert t.parse_docker_time("0001-01-01T00:00:00Z") is None
    assert t.parse_docker_time("не дата") is None
    assert t.parse_docker_time("") is None


# ── fmt_handshake ────────────────────────────────────────────────────────────
def test_fmt_handshake_never_and_old():
    assert t.fmt_handshake(None) == "никогда"
    assert t.fmt_handshake(0) == "никогда"
    old = t.fmt_handshake(int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()))
    assert "2020" in old


def test_fmt_handshake_online_now():
    fresh = int(t.now().timestamp()) - 10      # в пределах порога онлайна (300с)
    assert t.fmt_handshake(fresh) == "только что"
