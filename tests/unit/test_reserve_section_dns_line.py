"""Строка записи DNS в разделе «🛟 Резерв» без пары (bot/texts/reserve/section:
dns_line): состояния по настройке «🌐 DNS-запись» — вручную, провайдер без
токена, проверка не прошла, токен есть, но не проверена, проверена; у
проверенной — предупреждение при нескольких A-адресах или большом TTL
(данные — reserve_dns_line_data, те же, что у карточки пары). Время проверки
— с датой, если проверяли не сегодня: без пары проверка обычно давняя.

Цена ошибки: красная «не настроена» при настроенном и проверенном
провайдере — админ думает, что аварийный переезд не сработает, и лезет
перенастраивать то, что работает; зелёная при провале проверки — наоборот,
узнает о битом токене уже в аварии. Неэкранированное имя провайдера или
причина отказа (текст чужого API) ломают HTML-разметку всего экрана.
"""
from __future__ import annotations

import re

import pytest

from awgbot.bot.texts.reserve import section as st

pytestmark = pytest.mark.unit

from awgbot.util import timeutil

CHECKED = timeutil.to_iso(timeutil.now().replace(microsecond=0))         # сегодня
OLD = "2026-10-24T09:05:00+05:00"                                         # давняя проверка


def _dns(**over) -> dict:
    d = {"provider": "digitalplat", "provider_label": "DigitalPlat", "has_token": True, "check_ok": True,
         "checked_at": CHECKED, "check_why": "", "addrs": ["203.0.113.5"], "ttl": 60, "ttl_warn": 300}
    d.update(over)
    return d


def test_manual_provider_is_a_red_line_about_moving_by_button_only():
    """«Вручную» — бот запись не меняет: аварийного переезда нет, только кнопкой."""
    line = st.dns_line(_dns(provider="manual", provider_label="вручную", has_token=False, check_ok=None, checked_at=""))
    assert line == "🔴 DNS-запись: вручную — переезд только кнопкой, запись меняешь у провайдера сам"
    assert st.dns_line({}) == line, "пустая настройка — то же, что «вручную»: провайдер ещё не выбран"


def test_provider_without_a_token_is_red_and_points_to_the_dns_screen():
    """Провайдер выбран, токена нет — бот не сможет сменить запись; строка ведёт туда, где его задать."""
    line = st.dns_line(_dns(has_token=False, check_ok=None, checked_at=""))
    assert line.startswith("🔴 DNS-запись: DigitalPlat, токен не задан — задай его в «🌐 DNS-запись»"), line
    assert "«🌐 DNS-запись»" in line, "без подсказки, где задать токен, админ ищет его по всем разделам"


def test_failed_check_is_red_with_the_reason_and_a_default_when_the_provider_gave_none():
    """Проверка провалилась — причина из ответа провайдера; пустая причина — «провайдер отказал», не пустое место."""
    assert st.dns_line(_dns(check_ok=False, check_why="токен без права dns.write")) == \
        "🔴 DNS-запись: DigitalPlat — проверка не прошла: токен без права dns.write"
    assert st.dns_line(_dns(check_ok=False, check_why="")) == \
        "🔴 DNS-запись: DigitalPlat — проверка не прошла: провайдер отказал"


@pytest.mark.parametrize("over", [{"check_ok": None, "checked_at": ""}, {"check_ok": None},
                                  {"check_ok": True, "checked_at": ""}],
                         ids=["never-checked", "ok-none-with-stamp", "ok-true-without-stamp"])
def test_a_token_that_was_never_checked_is_a_warning_and_asks_to_run_the_check(over):
    """Токен есть, итога проверки нет — ⚠️ невыполненного действия (не 🟡: само не
    пройдёт, нужен шаг человека): бот не знает, годится ли токен, и просит нажать «🧪 Проверить»."""
    line = st.dns_line(_dns(**over))
    assert line == "⚠️ DNS-запись: DigitalPlat, токен задан — не проверена: «🌐 DNS-запись» → «🧪 Проверить»"


def test_a_checked_record_is_green_with_the_time_of_the_check():
    """Проверка прошла — зелёная строка со временем проверки: админ видит, что бот сменит запись сам и когда это подтверждено."""
    line = st.dns_line(_dns())
    assert line.startswith("🟢 DNS-запись: DigitalPlat — бот меняет её сам · проверено "), line
    assert re.search(r"проверено \d\d\.\d\d \d\d:\d\d \(UTC", line), f"даты и времени проверки нет: {line!r}"
    assert "не настроена" not in line and "🔴" not in line


def test_a_check_from_another_day_shows_its_date():
    """Без пары проверка обычно давняя: «проверено в 09:05» в четверг про понедельник
    читалось бы как сегодня — дата обязательна, если проверяли не сегодня."""
    line = st.dns_line(_dns(checked_at=OLD))
    assert re.search(r"проверено 24\.10 09:05", line), line
    assert "проверено в" not in line


def test_several_a_records_make_the_line_a_warning_not_green():
    """Два адреса у записи: «🧪 Проверить» предупредил один раз, а раздел дальше
    зеленел — при переезде часть пользователей осталась бы на прежнем адресе."""
    line = st.dns_line(_dns(addrs=["203.0.113.5", "198.51.100.7"]))
    assert line.startswith("⚠️ DNS-запись: DigitalPlat — у записи 2 адреса (203.0.113.5, 198.51.100.7), оставь одну A-запись"), line
    assert "🟢" not in line and "проверено " in line
    assert "5 адресов" in st.dns_line(_dns(addrs=[f"203.0.113.{i}" for i in range(5)]))


def test_a_big_ttl_makes_the_line_a_warning_with_the_delay():
    line = st.dns_line(_dns(ttl=3600))
    assert line.startswith("⚠️ DNS-запись: DigitalPlat — TTL 3600 с (рекомендуется ≤ 300): часть пользователей перейдёт на новый адрес не раньше чем через 60 мин"), line
    assert "🟢" not in line
    assert st.dns_line(_dns(ttl=300)).startswith("🟢"), "TTL на границе — не предупреждение"
    assert st.dns_line(_dns(ttl=0)).startswith("🟢"), "TTL не известен (0) — не предупреждение"


def test_a_broken_check_stamp_keeps_the_line_green_but_without_a_time():
    """Битая метка времени (мусор в состоянии) — строка остаётся зелёной и без «проверено в», а не падает экраном."""
    line = st.dns_line(_dns(checked_at="вчера"))
    assert line == "🟢 DNS-запись: DigitalPlat — бот меняет её сам"


def test_label_and_reason_are_html_escaped():
    """Имя провайдера и причина отказа идут в HTML: угловые скобки обязаны стать сущностями, иначе экран не отрисуется."""
    bad = _dns(provider_label="<b>Dns</b>", check_ok=False, check_why="ответ <403> & отказ")
    line = st.dns_line(bad)
    assert "<b>" not in line and "<403>" not in line, line
    assert "&lt;b&gt;Dns&lt;/b&gt;" in line and "&lt;403&gt; &amp; отказ" in line
    assert "<b>" not in st.dns_line(_dns(provider_label="<b>Dns</b>", has_token=False)), "экранирование нужно во всех ветках"
    assert "<b>" not in st.dns_line(_dns(provider_label="<b>Dns</b>", check_ok=None, checked_at=""))
    assert "<b>" not in st.dns_line(_dns(provider_label="<b>Dns</b>"))
    assert "<b>" not in st.dns_line(_dns(provider_label="<b>Dns</b>", addrs=["<b>", "x"]))
    assert "<b>" not in st.dns_line(_dns(provider_label="<b>Dns</b>", ttl=9000))


def test_without_ttl_warn_the_threshold_is_300():
    """Данные без порога (прежний сборщик, неполный словарь) — порог 300 с, как
    у экрана записи: без него любой TTL выше нуля стал бы предупреждением или,
    наоборот, ни один."""
    base = {k: v for k, v in _dns().items() if k != "ttl_warn"}
    assert st.dns_line({**base, "ttl": 300}).startswith("🟢"), "TTL 300 без порога в данных — уже предупреждение"
    line = st.dns_line({**base, "ttl": 301})
    assert line.startswith("⚠️ DNS-запись: DigitalPlat — TTL 301 с (рекомендуется ≤ 300)"), line


def test_one_address_is_green_and_does_not_ask_to_leave_one_record():
    line = st.dns_line(_dns(addrs=["203.0.113.5"]))
    assert line.startswith("🟢 DNS-запись: DigitalPlat — бот меняет её сам"), line
    assert "оставь одну A-запись" not in line and "203.0.113.5" not in line, line


def test_a_passed_check_without_addresses_is_green():
    """Проверка прошла, а адресов в состоянии нет (провайдер их не вернул или
    состояние до 4.1.0) — это не «несколько A-записей»: строка зелёная, а не
    падение и не ⚠️ на пустом месте."""
    for addrs in ([], None):
        line = st.dns_line(_dns(addrs=addrs))
        assert line.startswith("🟢 DNS-запись: DigitalPlat — бот меняет её сам · проверено "), (addrs, line)
    no_key = {k: v for k, v in _dns().items() if k not in ("addrs", "ttl")}
    assert st.dns_line(no_key).startswith("🟢"), "данные без addrs и ttl — не зелёная строка"
