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


# ── заголовок снимка и хвост синхронизации ──────────────────────────────────

def test_the_snapshot_title_says_not_added_only_when_the_result_is_a_summary():
    """От BATCH_SUMMARY_FROM доменов в снимок идут только пропущенные строки —
    заголовок «…, не добавленные»; меньше — в снимке все строки итога, хвост
    соврал бы. Служебные строки с отступом доменами не считаются."""
    n = tt.BATCH_SUMMARY_FROM
    below = _out(n - 1)                                   # n−1 доменов + строка с отступом
    at = _out(n - 1, 1)
    assert tt.transit_result_title("add", below) == "🔀 Итог: в туннель", "служебная строка посчитана доменом"
    assert tt.transit_result_title("add", at) == "🔀 Итог: в туннель, не добавленные"
    assert tt.transit_result_title("ru", at) == "🔀 Итог: напрямую, не добавленные"
    assert tt.transit_result_title("ru", below) == "🔀 Итог: напрямую"
    assert tt.transit_result_title("add", _out(200, 200)) == "🔀 Итог: в туннель, не добавленные"
    assert tt.transit_result_title("add") == "🔀 Итог: в туннель", "без вывода — без хвоста"
    assert tt.transit_result_title("add", "") == "🔀 Итог: в туннель"


def test_the_snapshot_title_of_a_removal_never_says_not_added():
    """Удаление сводкой не сворачивается (в снимке все строки) — «не добавленные» там ложь."""
    out = "\n".join(f"<code>d{i}.example</code>: убран" for i in range(50))
    assert tt.transit_result_title("del", out) == "🔀 Итог: в туннель", tt.transit_result_title("del", out)
    assert len(tt.transit_result_rows(True, out, "del")) == 50, "удаление свернулось в сводку — заголовок устарел"


@pytest.mark.parametrize("sync", ["online", "offline"])
def test_the_sync_tail_is_a_separate_paragraph_in_the_result_and_in_the_refusal(sync):
    """Хвост синхронизации — отдельным абзацем: прилепленный к последней строке
    перечня, он читается как её продолжение."""
    tail = tt.SYNC_TAILS[sync]
    ok = tt.gateway_transit_result(True, "<code>a.example</code>: добавлен", sync=sync)
    assert ok == f"✅ <code>a.example</code>: добавлен\n\n{tail}", ok
    big = tt.gateway_transit_result(True, _out(200, 200), sync=sync, budget=1200)
    assert big.endswith(f"строк\n\n{tail}") or big.endswith(f"строки\n\n{tail}") \
        or big.endswith(f"строка\n\n{tail}"), big[-160:]
    bad = tt.gateway_transit_result(False, "a.example: не похоже на домен", sync=sync, domains=["a.example"])
    assert bad.endswith(f"\n\n{tail}") and "\n\n\n" not in bad, bad
    assert tt.gateway_transit_result(True, "<code>a.example</code>: добавлен") == "✅ <code>a.example</code>: добавлен", \
        "без связи и без хвоста — пустой абзац в конце"
