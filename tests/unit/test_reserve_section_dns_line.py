"""Строка записи DNS в разделе «🛟 Резерв» без пары (bot/texts/reserve/section:
dns_line): пять состояний по настройке «🌐 Запись DNS» — вручную, провайдер
без токена, проверка не прошла, токен есть, но не проверена, проверена.

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

CHECKED = "2026-10-24T09:05:00+05:00"


def _dns(**over) -> dict:
    d = {"provider": "digitalplat", "label": "DigitalPlat", "has_token": True, "check_ok": True,
         "checked_at": CHECKED, "check_why": ""}
    d.update(over)
    return d


def test_manual_provider_is_a_red_line_about_moving_by_button_only():
    """«Вручную» — бот запись не меняет: аварийного переезда нет, только кнопкой."""
    line = st.dns_line(_dns(provider="manual", label="вручную", has_token=False, check_ok=None, checked_at=""))
    assert line == "🔴 Запись DNS: вручную — переезд только кнопкой, запись меняешь у провайдера сам"
    assert st.dns_line({}) == line, "пустая настройка — то же, что «вручную»: провайдер ещё не выбран"


def test_provider_without_a_token_is_red_and_points_to_the_dns_screen():
    """Провайдер выбран, токена нет — бот не сможет сменить запись; строка ведёт туда, где его задать."""
    line = st.dns_line(_dns(has_token=False, check_ok=None, checked_at=""))
    assert line.startswith("🔴 Запись DNS: DigitalPlat, токен не задан — переезд только кнопкой"), line
    assert "«🌐 Запись DNS»" in line, "без подсказки, где задать токен, админ ищет его по всем разделам"


def test_failed_check_is_red_with_the_reason_and_a_default_when_the_provider_gave_none():
    """Проверка провалилась — причина из ответа провайдера; пустая причина — «провайдер отказал», не пустое место."""
    assert st.dns_line(_dns(check_ok=False, check_why="токен без права dns.write")) == \
        "🔴 Запись DNS: DigitalPlat — проверка не прошла: токен без права dns.write"
    assert st.dns_line(_dns(check_ok=False, check_why="")) == \
        "🔴 Запись DNS: DigitalPlat — проверка не прошла: провайдер отказал"


@pytest.mark.parametrize("over", [{"check_ok": None, "checked_at": ""}, {"check_ok": None},
                                  {"check_ok": True, "checked_at": ""}],
                         ids=["never-checked", "ok-none-with-stamp", "ok-true-without-stamp"])
def test_a_token_that_was_never_checked_is_yellow_and_asks_to_run_the_check(over):
    """Токен есть, итога проверки нет — жёлтая: бот не знает, годится ли токен, и просит нажать «🧪 Проверить»."""
    line = st.dns_line(_dns(**over))
    assert line == "🟡 Запись DNS: DigitalPlat, токен задан — не проверена: «🌐 Запись DNS» → «🧪 Проверить»"


def test_a_checked_record_is_green_with_the_time_of_the_check():
    """Проверка прошла — зелёная строка со временем проверки: админ видит, что бот сменит запись сам и когда это подтверждено."""
    line = st.dns_line(_dns())
    assert line.startswith("🟢 Запись DNS: DigitalPlat — бот меняет её сам · проверено в "), line
    assert re.search(r"проверено в \d\d:\d\d", line), f"времени проверки нет: {line!r}"
    assert "не настроена" not in line and "🔴" not in line


def test_a_broken_check_stamp_keeps_the_line_green_but_without_a_time():
    """Битая метка времени (мусор в состоянии) — строка остаётся зелёной и без «проверено в», а не падает экраном."""
    line = st.dns_line(_dns(checked_at="вчера"))
    assert line == "🟢 Запись DNS: DigitalPlat — бот меняет её сам"


def test_label_and_reason_are_html_escaped():
    """Имя провайдера и причина отказа идут в HTML: угловые скобки обязаны стать сущностями, иначе экран не отрисуется."""
    bad = _dns(label="<b>Dns</b>", check_ok=False, check_why="ответ <403> & отказ")
    line = st.dns_line(bad)
    assert "<b>" not in line and "<403>" not in line, line
    assert "&lt;b&gt;Dns&lt;/b&gt;" in line and "&lt;403&gt; &amp; отказ" in line
    assert "<b>" not in st.dns_line(_dns(label="<b>Dns</b>", has_token=False)), "экранирование нужно во всех ветках"
    assert "<b>" not in st.dns_line(_dns(label="<b>Dns</b>", check_ok=None, checked_at=""))
    assert "<b>" not in st.dns_line(_dns(label="<b>Dns</b>"))
