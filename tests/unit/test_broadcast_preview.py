"""
Превью рассылки с продлением (bot/texts/broadcast): подвал «кому» на много
профилей и отказ Telegram по длине.

Цена ошибки: подвал по строке на профиль пробивал 4096 уже на ~60 профилях с
длинным текстом — Bot API отказывал, и разослать с продлением всем было
нельзя, а причина называлась «разметка бракованная».
"""
from __future__ import annotations

import datetime as _dt
import re
from types import SimpleNamespace

import pytest

from awgbot.bot.texts import broadcast as tb
from awgbot.core import config

pytestmark = pytest.mark.unit

_TAGS = re.compile(r"<[^>]+>")


def _plan(n: int):
    base = _dt.datetime(2026, 11, 1, 12, 0, tzinfo=config.TZ)
    return [SimpleNamespace(client=SimpleNamespace(id=i, tg_id=5000 + i, name=f"Профиль с длинным именем {i:03d}"),
                            unlimited=False, from_now=i % 7 == 0, old_end=base, new_end=base + _dt.timedelta(days=30))
            for i in range(n)]


def test_the_extension_footer_shows_twelve_profiles_and_counts_the_rest():
    foot = tb.broadcast_preview("т" * 400, 150, extension=(30, _plan(150)), bot_username="test_bot")
    lines = foot.split("\n")
    assert lines[0].endswith("150 адресатов · продление на <b>30 дней</b>:"), lines[0]
    dated = [ln for ln in lines if ln.startswith("• ")]
    assert len(dated) == tb.NAMES_SHOWN, len(dated)
    assert not dated[-1].endswith("]") and "… и ещё 138" in lines, lines[-3:]   # счёт своей строкой, не к датам
    assert len(_TAGS.sub("", foot)) <= config.TG_TEXT_MAX, len(_TAGS.sub("", foot))


def test_the_extension_footer_of_twelve_or_fewer_has_no_counter():
    foot = tb.broadcast_preview_photos(12, extension=(7, _plan(12)))
    assert "и ещё" not in foot and foot.count("\n• ") == 12, foot


@pytest.mark.parametrize("err, head", [
    ("Telegram server says - Bad Request: message is too long", "⚠️ Превью не отправлено: текст длиннее лимита Telegram"),
    ("Bad Request: can't parse entities: unclosed tag", "⚠️ Разметка бракованная"),
])
def test_the_refusal_of_a_preview_is_named_by_its_cause(err, head):
    assert tb.broadcast_preview_refused(err).startswith(head), tb.broadcast_preview_refused(err)


@pytest.mark.parametrize("n, head", [(1, "1 адресат"), (21, "21 адресат"), (22, "22 адресата"), (25, "25 адресатов"),
                                     (11, "11 адресатов"), (112, "112 адресатов")])
def test_the_extension_footer_head_agrees_with_the_number(n, head):
    """«21 адресатов» — склонение по последней цифре, а не «1 / 2–4 / прочее»."""
    foot = tb._extension_footer(30, _plan(n), n)
    assert foot.split("\n")[0] == f"{head} · продление на <b>30 дней</b>:", foot.split("\n")[0]


def test_the_rest_counter_is_its_own_last_line_and_the_last_dates_stay_clean():
    """150 адресатов: двенадцать строк с датами, последняя кончается датой; счёт
    остатка — отдельной строкой в самом конце, к «01.11 → 01.12» не приклеен
    (иначе он читается как часть срока последнего профиля)."""
    lines = tb._extension_footer(30, _plan(150), 150).split("\n")
    assert lines[-1] == "… и ещё 138", lines[-3:]
    dated = lines[1:-1]
    assert len(dated) == tb.NAMES_SHOWN and all(ln.startswith("• ") for ln in dated), dated
    assert re.search(r"→ \d\d\.\d\d(\.\d\d)?$", dated[-1]), f"после даты последнего профиля хвост: {dated[-1]!r}"
