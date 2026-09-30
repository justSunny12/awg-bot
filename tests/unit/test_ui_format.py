"""Unit: общие правила экранов — формат дат без текущего года, давность и
остаток коротко, массовый выбор ☑️/✅, «Лимит исчерпан: удали N», пресеты
лимита устройства не выше лимита профиля, разбор ссылок /start клиента и
гостя.

Цена ошибки: дата с лишним годом или секундами — шум в каждой строке
каждого экрана; «удали 1» при пяти из трёх — человек удаляет, а добавить
всё равно не может; пресет выше лимита профиля — кнопка, которую сервис
отвергнет; ссылка, принятая за код, — «такого кода нет» вместо экрана.
"""
from datetime import datetime, timedelta, timezone

import pytest

from awgbot.bot import keyboards as kb
from awgbot.bot import texts
from awgbot.bot.callbacks import RoutingCB
from awgbot.core import settings
from awgbot.util import timeutil as t

pytestmark = pytest.mark.unit
TZ = t.TZ
G = 1024 ** 3
REF = datetime(2026, 9, 27, 12, 0, 0, tzinfo=TZ)


def _dt(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ)


# ── даты ─────────────────────────────────────────────────────────────────────

def test_current_year_is_not_written():
    assert t.fmt_dt_ui(_dt(2026, 10, 12, 18, 0), REF) == "12.10 18:00"
    assert t.fmt_date_ui(_dt(2026, 10, 12, 18, 0), REF) == "12.10"


@pytest.mark.parametrize("year, shown", [(2027, "12.10.27"), (2025, "12.10.25")])
def test_other_year_is_two_digits(year, shown):
    """Год — двумя цифрами и только когда он не текущий: и в будущем (срок
    до следующего года), и в прошлом (начало годовой подписки)."""
    assert t.fmt_date_ui(_dt(year, 10, 12), REF) == shown
    assert t.fmt_dt_ui(_dt(year, 10, 12, 18, 5), REF) == f"{shown} 18:05"


def test_year_is_taken_in_moscow_time_not_utc():
    """31.12 23:30 UTC — уже 01.01 по UTC+3: и дата, и «текущий ли год»
    считаются по времени экрана."""
    utc = datetime(2026, 12, 31, 23, 30, tzinfo=timezone.utc)
    assert t.fmt_dt_ui(utc, _dt(2027, 1, 1, 10)) == "01.01 02:30"
    assert t.fmt_dt_ui(utc, REF) == "01.01.27 02:30"


def test_seconds_only_on_request_and_only_when_not_zero():
    assert t.fmt_dt_ui(_dt(2026, 10, 12, 18, 0, 42), REF) == "12.10 18:00", "секунды без просьбы"
    assert t.fmt_dt_ui(_dt(2026, 10, 12, 18, 0, 42), REF, seconds=True) == "12.10 18:00:42"
    assert t.fmt_dt_ui(_dt(2026, 10, 12, 18, 0, 0), REF, seconds=True) == "12.10 18:00", ":00 не пишем"


def test_period_is_start_arrow_end_with_the_same_year_rule():
    """Границы в разных годах — год у обеих: «12.10.25 → 12.10» читается как
    «до 12.10 этого года», и годовую подписку примут за истекающую."""
    now_year = t.now().year
    start, end = _dt(now_year - 1, 10, 12), _dt(now_year, 10, 12)
    yy0, yy1 = f"{(now_year - 1) % 100:02d}", f"{now_year % 100:02d}"
    assert t.fmt_period_ui(start, end) == f"12.10.{yy0} → 12.10.{yy1}"
    # начало в текущем году, конец в следующем — тоже оба с годом
    start, end = _dt(now_year, 10, 12), _dt(now_year + 1, 10, 12)
    assert t.fmt_period_ui(start, end) == f"12.10.{yy1} → 12.10.{(now_year + 1) % 100:02d}", \
        "год у начала не должен теряться, даже если это текущий год"
    # оба в текущем году — без года
    start, end = _dt(now_year, 1, 12), _dt(now_year, 10, 12)
    assert t.fmt_period_ui(start, end) == "12.01 → 12.10"


@pytest.mark.parametrize("raw, parsed", [
    ("12.10.2026 18:30", _dt(2026, 10, 12, 18, 30)),
    ("12.10.2026", _dt(2026, 10, 12, 0, 0)),               # без времени — 00:00
    ("  12.10.2026   18:30 ", _dt(2026, 10, 12, 18, 30)),   # лишние пробелы
    ("12.10.2026 18:30:15", _dt(2026, 10, 12, 18, 30, 15)),
])
def test_date_input_full_year_with_or_without_time(raw, parsed):
    assert t.parse_dt_sec(raw) == parsed


@pytest.mark.parametrize("raw", ["12.10", "12.10.26 18:30", "2026-10-12", "", "завтра"])
def test_date_input_rejects_what_the_prompt_does_not_offer(raw):
    with pytest.raises(ValueError):
        t.parse_dt_sec(raw)


# ── давность и остаток ───────────────────────────────────────────────────────

def test_ago_in_words():
    online = settings.get_int("app.online_handshake_seconds", 300)
    assert online < 20 * 60, "порог онлайна вырос — пересчитай отрезки теста"
    ts = int(REF.timestamp())
    assert t.fmt_ago(None, REF) == "никогда" and t.fmt_ago(0, REF) == "никогда"
    assert t.fmt_ago(ts - 30, REF) == "только что"
    assert t.fmt_ago(ts - 20 * 60, REF) == "20 мин назад"
    assert t.fmt_ago(ts - 3 * 3600 - 59, REF) == "3 ч назад"
    assert t.fmt_ago(ts - 30 * 3600, REF) == "вчера"
    assert t.fmt_ago(int(_dt(2026, 9, 12, 10).timestamp()), REF) == "12.09"
    assert t.fmt_ago(int(_dt(2025, 9, 12, 10).timestamp()), REF) == "12.09.25"


def test_remaining_brief_rounds_days_up_and_says_expired():
    assert t.remaining_brief(REF + timedelta(days=2, hours=3), REF) == "3 дн.", "дни — вверх"
    assert t.remaining_brief(REF + timedelta(days=18), REF) == "18 дн."
    assert t.remaining_brief(REF + timedelta(hours=11, minutes=50), REF) == "11 ч"
    assert t.remaining_brief(REF + timedelta(minutes=40), REF) == "40 мин"
    assert t.remaining_brief(REF + timedelta(seconds=20), REF) == "1 мин", "не «0 мин» при живой подписке"
    assert t.remaining_brief(REF, REF) == "истекло"
    assert t.remaining_brief(REF - timedelta(days=1), REF) == "истекло"


# ── массовый выбор ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("selected, total, mark", [
    (0, 3, "☑️"), (2, 3, "☑️"),        # пока выбраны не все
    (3, 3, "✅"),                       # все — в том числе отмеченные по одному
    (0, 0, "☑️"),                       # пустой список — не «всё выбрано»
])
def test_select_all_mark_follows_the_selection(selected, total, mark):
    b = kb.select_all_button(selected, total, RoutingCB(action="all", ref=5))
    assert b.text == f"{mark} Выбрать все"
    assert RoutingCB.unpack(b.callback_data) == RoutingCB(action="all", ref=5)


def test_routing_panel_turns_select_all_green_when_every_device_is_ticked_by_hand():
    """На экране РФ-доступа: включили все по одному — «Выбрать все» ✅."""
    from types import SimpleNamespace
    devs = [SimpleNamespace(id=i, name=f"D{i}", routing_on=on, is_managed=True, is_lent=False)
            for i, on in ((1, 1), (2, 1))]
    labels = [b.text for r in kb.routing_panel(1, devs, enabled=2, total=2,
                                               back_target="m:main").inline_keyboard for b in r]
    assert labels[:3] == ["✅ D1", "✅ D2", "✅ Выбрать все"], labels
    devs[1].routing_on = 0
    labels = [b.text for r in kb.routing_panel(1, devs, enabled=1, total=2,
                                               back_target="m:main").inline_keyboard for b in r]
    assert labels[:3] == ["✅ D1", "☑️ D2", "☑️ Выбрать все"], labels


# ── «Лимит исчерпан: удали N» ────────────────────────────────────────────────

@pytest.mark.parametrize("used, limit, line", [
    (3, 3, "Лимит исчерпан: чтобы добавить новое, удали 1"),
    (5, 3, "Лимит исчерпан: чтобы добавить новое, удали 3"),    # после понижения лимита
    (2, 3, ""),                                                   # место есть
    (7, 0, ""),                                                   # без лимита
])
def test_limit_exhausted_asks_to_delete_enough(used, limit, line):
    assert texts.limit_exhausted_line(used, limit) == line


def test_devices_header_carries_the_exhausted_line():
    assert texts.devices_header(5, 3) == ("📱 <b>Устройства</b> · 5 из 3\n"
                                          "Лимит исчерпан: чтобы добавить новое, удали 3")
    assert texts.devices_header(2, 3) == "📱 <b>Устройства</b> · 2 из 3"


# ── пресеты лимита устройства ────────────────────────────────────────────────

@pytest.mark.parametrize("profile_gb, presets", [
    (100, [10, 50, 100]),
    (0, [10, 50, 100, 0]),       # ∞ — только у безлимитного профиля
    (5, [5]),                    # ниже всех пресетов — сам лимит профиля
    (30, [10, 30]),              # лимит профиля между пресетами — он и есть потолок
    (500, [10, 50, 100, 500]),  # сам лимит профиля — всегда среди пресетов
])
def test_device_limit_presets_never_exceed_the_profile(profile_gb, presets):
    assert kb.device_limit_presets(profile_gb * G) == presets


def test_device_limit_keyboard_labels_and_exit():
    from awgbot.bot.callbacks import DeviceCB, PresetCB
    mk = kb.device_limit_kb(7, 100 * G, DeviceCB(action="open", device_id=7))
    rows = [[b.text for b in r] for r in mk.inline_keyboard]
    assert rows == [["10 ГБ", "50 ГБ", "100 ГБ"], ["✏️ Другое"], ["⬅️ Отмена"]], rows
    unlimited = [[b.text for b in r] for r in kb.device_limit_kb(7, 0, "m:main").inline_keyboard]
    assert unlimited == [["10 ГБ", "50 ГБ", "100 ГБ"], ["∞"], ["✏️ Другое"], ["⬅️ Отмена"]], unlimited
    vals = [PresetCB.unpack(b.callback_data).val for r in mk.inline_keyboard[:2] for b in r]
    assert vals == [10, 50, 100, -1], "«✏️ Другое» — val=-1"


# ── ссылки /start клиента и гостя ────────────────────────────────────────────

@pytest.mark.parametrize("payload, link", [
    ("sub", ("sub", 0)), ("rf", ("rf", 0)), ("dev-12", ("dev", 12)),
    ("dev-", None), ("dev-x1", None), ("dev12", None), ("sub-1", None),
    ("C1a2b3c4d5e6", None), ("F1a2b3c4d5e6", None), ("", None),
])
def test_parse_link_tells_screens_from_codes(payload, link):
    """Коды приглашений — 12 знаков без «-»: разбор ссылки их не трогает, они
    уходят в активацию как раньше."""
    from awgbot.bot.handlers.client import parse_link
    assert parse_link(payload) == link


def test_volumes_switch_to_terabytes_from_a_terabyte():
    """Объёмы от терабайта — в ТБ, единица одна на пару «из»; ниже — ГБ, как
    было; безлимит — «0 ГБ (безлимит)», а не пусто."""
    from awgbot.bot import texts
    from awgbot.bot.texts import client as tc
    TB, GB = 1024 ** 4, 1024 ** 3
    assert texts.human_bytes(2 * TB) == "2 ТБ" and texts.human_bytes(500 * GB) == "500 ГБ"
    assert texts.gb_str(int(1.5 * TB)) == "1.5 ТБ"
    assert texts.used_of_limit(TB // 2, 2 * TB) == "0.5 из 2 ТБ"
    assert texts.used_of_limit(3 * GB, 2 * TB) == "0 из 2 ТБ" or texts.used_of_limit(3 * GB, 2 * TB) == "0.01 из 2 ТБ"
    assert texts.used_of_limit(3 * GB, 50 * GB) == "3 из 50 ГБ"
    assert tc.traffic_short(0, 0, 0) == "📊 0 ГБ (безлимит)"
    assert tc.traffic_short(TB, TB, 4 * TB) == "📊 2 из 4 ТБ"
