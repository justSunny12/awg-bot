"""
Итог «➕ В туннель» / «➕ Напрямую» агента (bot/texts/gateway/transit): один
разбор вывода скрипта для экрана и снимка, обрезка — числом, ввод человека в
строке «не похоже на домен» экранируется до обёртки в <code>.

Цена ошибки: экран и снимок, собранные двумя разборами, показывают разные
строки; обрезка, найденная по подстроке в готовом тексте, молча пропадает
после вычитки формулировки; «<code>» из ввода человека, вернувшийся в
разметку, — отказ Bot API, и итог не приходит вовсе.
"""
from __future__ import annotations

import pytest

from awgbot.bot.texts.gateway import transit as tt

pytestmark = pytest.mark.unit


def _out(added: int, bad: int = 0) -> str:
    rows = [f"<code>d{i:03d}.example</code>: добавлен" for i in range(added)]
    rows += [f"bad-{i:03d}: не похоже на домен, пропущен" for i in range(bad)]
    rows.append("  адреса в наборе: 12")                            # служебная строка — с отступом
    return "\n".join(rows)


def test_the_screen_rows_are_a_prefix_of_the_snapshot_rows_and_the_cut_is_their_difference():
    out = _out(200, 200)
    text = tt.gateway_transit_result(True, out, budget=1200)
    lines = text.split("\n")
    assert lines[0] == "✅ Добавлено: 200 · пропущено: 200", lines[0]
    shown = lines[1:-1]
    rows = tt.transit_result_rows(True, out)
    cut = tt.transit_result_cut(True, out, budget=1200)
    assert rows[:len(shown)] == shown and len(shown) + cut == len(rows) == 200, (len(shown), cut)
    from awgbot.bot.texts.fmt import plural_ru
    assert lines[-1] == f"…и ещё {cut} " + plural_ru(cut, "строка", "строки", "строк"), lines[-1]
    assert tt.transit_result_cut(True, out, budget=100000) == 0


def test_the_cut_tail_is_a_link_to_the_snapshot_when_given():
    text = tt.gateway_transit_result(True, _out(200, 200), budget=1200, link=("test_bot", "list-snap-k3x9a2bq7d"))
    assert text.split("\n")[-1].startswith('…<a href="https://t.me/test_bot?start=list-snap-k3x9a2bq7d">и ещё '), text[-120:]


@pytest.mark.parametrize("raw", ["<code>", "x<code>y", "a&b</code>"])
def test_human_input_in_the_not_a_domain_line_is_escaped_before_the_code_wrap(raw):
    out = "\n".join([f"<code>d{i}.example</code>: добавлен" for i in range(6)] + [f"{raw}: не похоже на домен, пропущен"])
    from awgbot.bot.texts.fmt import _e
    want = f"⚠️ <code>{_e(raw)}</code>: не домен, пропущен"
    assert tt.transit_result_rows(True, out) == [want]
    assert tt.gateway_transit_result(True, out).split("\n")[1] == want
    assert "<code><code>" not in tt.gateway_transit_result(True, out)


def test_a_short_result_lists_every_line_with_its_icon():
    out = "<code>a.example</code>: добавлен\n<code>b.example</code>: уже в списке\nzzz: не похоже на домен, пропущен"
    assert tt.gateway_transit_result(True, out).split("\n") == [
        "✅ <code>a.example</code>: добавлен", "ℹ️ <code>b.example</code>: уже в списке",
        "⚠️ <code>zzz</code>: не домен, пропущен"]
    assert tt.transit_result_cut(False, out) == 0 and tt.transit_result_rows(False, out) == []
