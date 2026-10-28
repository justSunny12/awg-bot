"""Unit: awgbot.util.timeutil — пояс показа (в тестах — Asia/Yekaterinburg,
conftest), хранение UTC+3, метка пояса, арифметика через летнее время,
периоды, форматтеры (чистая логика)."""
from datetime import datetime, timezone

import pytest

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
    """Наивная строка (наследие) — время пояса хранения UTC+3, показ — в поясе бота."""
    got = t.parse_iso("2026-07-05T19:08:02")
    assert got == datetime(2026, 7, 5, 19, 8, 2, tzinfo=t.STORE_TZ)
    assert got == _dt(2026, 7, 5, 21, 8, 2) and got.tzinfo is TZ


def test_to_iso_converts_from_other_tz():
    utc = datetime(2026, 7, 5, 16, 8, 2, tzinfo=timezone.utc)   # = 19:08 UTC+3 = 21:08 UTC+5
    assert t.to_iso(utc) == "2026-07-05T19:08:02+03:00"
    assert t.parse_iso(t.to_iso(utc)) == _dt(2026, 7, 5, 21, 8, 2)


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


# ── время старта сервиса (ISO, суффикс Z) ─────────────────────────────────
def test_parse_started_at_valid_and_tz():
    got = t.parse_started_at("2026-07-05T10:39:22.158372221Z")   # 10:39 UTC = 15:39 UTC+5
    assert got is not None and got.hour == 15 and got.minute == 39


def test_parse_started_at_never_and_garbage():
    assert t.parse_started_at("0001-01-01T00:00:00Z") is None
    assert t.parse_started_at("не дата") is None
    assert t.parse_started_at("") is None


# ── fmt_handshake ────────────────────────────────────────────────────────────
def test_fmt_handshake_never_and_old():
    assert t.fmt_handshake(None) == "никогда"
    assert t.fmt_handshake(0) == "никогда"
    old = t.fmt_handshake(int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()))
    assert old == "01.01.20 05:00 (UTC+5)", "год — двумя цифрами, метка пояса"


def test_fmt_handshake_online_now():
    fresh = int(t.now().timestamp()) - 10      # в пределах порога онлайна (300с)
    assert t.fmt_handshake(fresh) == "только что"


def test_online_threshold_is_keyword_only():
    """Порог передаётся ТОЛЬКО именем. Позиционно вторым идёт опорное время, и
    число, прилетевшее туда, роняло проверку с AttributeError — а через неё
    ходят карточка профиля и экран «Управлять подпиской»."""
    import pytest as _pytest
    from awgbot.util import timeutil as tu
    now_ts = int(tu.now().timestamp())
    assert tu.handshake_is_online(now_ts - 10, threshold=300)
    assert not tu.handshake_is_online(now_ts - 10, threshold=5)
    assert not tu.handshake_is_online(0, threshold=300)
    with _pytest.raises(TypeError):
        tu.handshake_is_online(now_ts, 300, 300)      # третьего позиционного нет
    # и опорное время по-прежнему работает как опорное время
    assert tu.handshake_is_online(now_ts - 10, tu.now())



# ── пояс хранения — UTC+3 при любом поясе показа ─────────────────────────────
@pytest.fixture()
def berlin(monkeypatch):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Berlin")
    monkeypatch.setattr(t, "TZ", tz)
    return tz


def test_storage_is_utc3_whatever_the_display_zone(berlin):
    summer = datetime(2026, 7, 5, 12, 0, tzinfo=berlin)           # 12:00 UTC+2
    assert t.to_iso(summer) == "2026-07-05T13:00:00+03:00"
    assert t.now_iso().endswith("+03:00")
    got = t.parse_iso("2026-07-05T13:00:00+03:00")
    assert got == summer and got.tzinfo is berlin and got.hour == 12
    assert t.parse_iso("2026-07-05T10:00:00+00:00").hour == 12, "любое смещение читается"
    assert t.to_iso(datetime(2026, 7, 5, 12, 0)) == "2026-07-05T13:00:00+03:00", "наивное — стенные часы показа"


def test_storage_offset_in_the_pinned_zone():
    assert t.to_iso(_dt(2026, 9, 15, 12)) == "2026-09-15T10:00:00+03:00"
    assert t.now_iso().endswith("+03:00")


# ── метка пояса ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,at,label", [
    ("Europe/Moscow", (2026, 7, 1), "UTC+3"),
    ("Asia/Yekaterinburg", (2026, 7, 1), "UTC+5"),
    ("Asia/Kolkata", (2026, 7, 1), "UTC+5:30"),
    ("Asia/Kathmandu", (2026, 7, 1), "UTC+5:45"),
    ("America/St_Johns", (2026, 1, 15), "UTC-3:30"),
    ("Etc/UTC", (2026, 7, 1), "UTC"),
    ("Europe/London", (2026, 1, 15), "UTC"),
    ("Pacific/Kiritimati", (2026, 7, 1), "UTC+14"),
    ("Etc/GMT+12", (2026, 7, 1), "UTC-12"),
    ("Europe/Berlin", (2026, 7, 1), "UTC+2"),
    ("Europe/Berlin", (2026, 1, 15), "UTC+1"),
])
def test_tz_label(monkeypatch, name, at, label):
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(t, "TZ", ZoneInfo(name))
    assert t.tz_label(datetime(*at, 12, tzinfo=timezone.utc)) == label
    assert t.tz_tail(datetime(*at, 12, tzinfo=timezone.utc)) == f" ({label})"


def test_tz_label_now_and_explicit_zone():
    from zoneinfo import ZoneInfo
    assert t.tz_label() == "UTC+5"
    assert t.tz_label(tz=ZoneInfo("Europe/Moscow")) == "UTC+3"


def test_tz_label_is_of_the_shown_moment_not_of_now(berlin):
    """Летнее время: у двух моментов одного экрана метки разные."""
    assert t.tz_label(datetime(2026, 10, 24, 12, tzinfo=berlin)) == "UTC+2"
    assert t.tz_label(datetime(2026, 10, 26, 18, tzinfo=berlin)) == "UTC+1"


# ── арифметика через переход на летнее время ─────────────────────────────────
def test_elapsed_counts_real_seconds_over_dst(berlin):
    """25.10.2026 01:00 → 04:00 по стенным часам Берлина — 4 настоящих часа
    (вычитание дат одного пояса дало бы 3)."""
    a = datetime(2026, 10, 25, 1, 0, tzinfo=berlin)
    b = datetime(2026, 10, 25, 4, 0, tzinfo=berlin)
    assert (b - a).total_seconds() == 3 * 3600, "предпосылка: вычитание — по стенным часам"
    assert t.elapsed(a, b) == 4 * 3600
    assert t.remaining_seconds(b, a) == 4 * 3600
    assert t.period_minutes(a, b) == 240
    spring_a = datetime(2026, 3, 29, 1, 0, tzinfo=berlin)
    spring_b = datetime(2026, 3, 29, 4, 0, tzinfo=berlin)
    assert t.elapsed(spring_a, spring_b) == 2 * 3600


def test_elapsed_default_is_now_and_shift_is_absolute(monkeypatch, berlin):
    ref = datetime(2026, 10, 25, 4, 0, tzinfo=berlin)
    monkeypatch.setattr(t, "now", lambda: ref)
    assert t.elapsed(datetime(2026, 10, 25, 1, 0, tzinfo=berlin)) == 4 * 3600
    back = t.shift(ref, -4 * 3600)
    assert (back.hour, back.utcoffset().total_seconds()) == (1, 7200)
    assert back.tzinfo is berlin


def test_uptime_over_dst(monkeypatch, berlin):
    monkeypatch.setattr(t, "now", lambda: datetime(2026, 10, 25, 4, 0, tzinfo=berlin))
    assert t.fmt_uptime(datetime(2026, 10, 25, 1, 0, tzinfo=berlin)) == "4 часа"


def test_add_period_keeps_the_wall_clock_over_dst(berlin):
    start = datetime(2026, 10, 24, 18, 0, tzinfo=berlin)
    end = t.add_period(start, "day")
    assert (end.day, end.hour) == (25, 18) and t.tz_label(end) == "UTC+1"
    assert t.add_period(datetime(2026, 3, 1, 18, tzinfo=berlin), "month").hour == 18


# ── ввод: пропавший и повторный час ──────────────────────────────────────────
def test_input_in_the_missing_hour_moves_forward(berlin):
    got = t.parse_dt_sec("29.03.2026 02:30")
    assert (got.hour, got.minute, t.tz_label(got)) == (3, 30, "UTC+2")
    assert t.to_iso(got) == "2026-03-29T04:30:00+03:00"


def test_input_in_the_repeated_hour_is_the_first_one(berlin):
    got = t.parse_dt_sec("25.10.2026 02:30")
    assert (got.hour, got.minute, t.tz_label(got)) == (2, 30, "UTC+2")
    assert t.to_iso(got) == "2026-10-25T03:30:00+03:00"


def test_input_is_the_wall_clock_of_the_display_zone():
    got = t.parse_dt_sec("01.02.2027 18:30")
    assert (got.hour, got.minute, got.tzinfo) == (18, 30, TZ)
    assert t.to_iso(got) == "2027-02-01T16:30:00+03:00"


# ── дата с годом всегда ──────────────────────────────────────────────────────
def test_fmt_date_yy_has_the_year_even_in_the_current_year(monkeypatch):
    """Строки журнала данных: год — всегда, двумя цифрами (в отличие от экранов)."""
    monkeypatch.setattr(t, "now", lambda: _dt(2026, 11, 20, 12))
    assert t.fmt_date_yy(_dt(2026, 11, 15, 9)) == "15.11.26"
    assert t.fmt_date_ui(_dt(2026, 11, 15, 9)) == "15.11", "условие: экранная дата текущего года — без года"
    assert t.fmt_date_yy(_dt(2027, 1, 2)) == "02.01.27"


def test_fmt_date_yy_is_the_date_in_the_display_zone():
    """Момент в другом поясе — дата по поясу показа: 31.12 22:30 UTC — уже 01.01
    следующего года у админа в UTC+5; хранение (UTC+3) — тоже переводится."""
    assert t.fmt_date_yy(datetime(2026, 12, 31, 22, 30, tzinfo=timezone.utc)) == "01.01.27"
    assert t.fmt_date_yy(t.parse_iso("2026-03-09T21:30:00+03:00")) == "09.03.26"
    assert t.fmt_date_yy(t.parse_iso("2026-03-09T22:30:00+03:00")) == "10.03.26"
