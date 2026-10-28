"""
Где кончается «поимённо» и начинается хвост «и ещё N» у текстов, чей список
уходит снимком: отчёт рассылки (texts/broadcast.undelivered_tail), итог
«Свои сайты» (texts/routing/access.rejected_by_reason), итог «➕ В туннель»
агента (texts/gateway/transit.transit_result_cut).

Обработчик решает по этим числам, писать ли снимок, а текст по ним же рисует
хвост. Цена ошибки: число хвоста и решение обработчика разошлись — в отчёте
«и ещё 2» ссылкой на снимок, которого нет (или снимок есть, а ссылки на него
нет, и остаток списка не увидеть нигде); у одних гостей — ссылка «и ещё N» на
снимок из одной строки «N адресатов без профиля» вместо простого числа;
причины отказов в итоге и в решении сгруппированы по-разному — снимок пишется
не тогда, когда в итоге стоит «и ещё N».
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from awgbot.bot.texts import broadcast as tb
from awgbot.bot.texts.gateway import transit as tt
from awgbot.bot.texts.routing import access as ta

pytestmark = pytest.mark.unit

BOT = "test_bot"
_A = re.compile(r"<a [^>]+>([^<]+)</a>")


def _cl(i: int, name: str = "") -> SimpleNamespace:
    return SimpleNamespace(id=i, tg_id=1000 + i, name=name or f"Профиль {i}")


def _undelivered_line(clients, failed, more: str = "") -> str:
    report = tb.broadcast_report(clients, False, 10, failed, bot_username=BOT, undelivered_more=more)
    return next(ln for ln in report.split("\n") if ln.startswith("⚠️ Не доставлено:"))


# ── отчёт рассылки: недоставленные ──────────────────────────────────────────

def test_only_guests_undelivered_have_no_linked_tail_and_the_report_shows_their_number():
    """Не дошло только гостям (чужие tg, без профиля): назвать некого — в отчёте
    просто число, хвоста со ссылкой нет, и обработчик снимок не пишет."""
    clients = [_cl(1), _cl(2)]
    failed = [9001, 9002, 9003]
    assert tb.undelivered_tail(failed, clients) == 0, "у одних гостей — хвост «и ещё N» со снимком"
    line = _undelivered_line(clients, failed)
    assert line.startswith("⚠️ Не доставлено: 3 — бот заблокирован"), line
    assert "и ещё" not in line and "<a " not in line, f"у одних гостей — хвост или ссылка: {line}"


def test_three_named_and_guests_put_exactly_the_guests_into_the_tail():
    """Трое с профилем названы поимённо, двое гостей — «и ещё 2»: хвост ровно
    на тех, кого в строке нет."""
    clients = [_cl(1), _cl(2), _cl(3), _cl(4)]
    failed = [1001, 1002, 1003, 9001, 9002]
    assert tb.undelivered_tail(failed, clients) == 2, tb.undelivered_tail(failed, clients)
    line = _undelivered_line(clients, failed)
    assert _A.findall(line) == ["Профиль 1", "Профиль 2", "Профиль 3"], line
    assert " и ещё 2 — бот заблокирован" in line, line


def test_the_linked_tail_replaces_the_text_tail_and_counts_the_same():
    """Ссылка на снимок (undelivered_more) встаёт вместо текстового «и ещё N»,
    не рядом с ним."""
    clients = [_cl(i) for i in range(1, 6)]
    failed = [1001, 1002, 1003, 1004, 1005, 9001]
    rest = tb.undelivered_tail(failed, clients)
    assert rest == 3, rest
    more = f' <a href="https://t.me/{BOT}?start=list-snap-k3x9a2bq7d">и ещё {rest}</a>'
    line = _undelivered_line(clients, failed, more)
    assert line.count("и ещё") == 1 and f">и ещё {rest}</a> — бот" in line, line


@pytest.mark.parametrize("named, guests, tail", [(0, 0, 0), (1, 0, 0), (3, 0, 0), (4, 0, 1), (1, 4, 4), (2, 1, 1)])
def test_the_tail_is_everyone_not_named_and_zero_when_nobody_is_left_or_named(named, guests, tail):
    clients = [_cl(i) for i in range(1, 8)]
    failed = [1000 + i for i in range(1, named + 1)] + [9000 + g for g in range(guests)]
    assert tb.undelivered_tail(failed, clients) == tail, (named, guests, tb.undelivered_tail(failed, clients))


# ── «Свои сайты»: отказы по причинам ────────────────────────────────────────

def test_rejections_are_grouped_by_reason_in_the_order_the_report_shows_them():
    """Порядок причин — порядок первого появления, ровно как строки итога:
    обработчик решает «писать ли снимок» по тем же группам, что рисует текст."""
    rejected = [("a..ru", "не похоже на адрес"), ("10.0.0.1", "адрес локальной сети"),
                ("b..ru", "не похоже на адрес"), ("x", "нужен адрес вида sberbank.ru"),
                ("10.0.0.2", "адрес локальной сети")]
    by = ta.rejected_by_reason(rejected)
    assert list(by) == ["не похоже на адрес", "адрес локальной сети", "нужен адрес вида sberbank.ru"], list(by)
    assert by["не похоже на адрес"] == ["a..ru", "b..ru"] and by["адрес локальной сети"] == ["10.0.0.1", "10.0.0.2"]
    report = ta.routing_add_report([], rejected, 0, 100)
    shown = [ln.rsplit(" — ", 1)[1] for ln in report.split("\n") if ln.startswith("⚠️ Не добавлено:")]
    assert shown == ["не похоже на адрес", "адрес локальной сети", "нужен адрес вида <code>sberbank.ru</code>"], shown


def test_no_rejections_make_no_groups():
    assert ta.rejected_by_reason([]) == {}


# ── «➕ В туннель» агента: сколько строк не влезло ────────────────────────────

def _del_out(n: int) -> str:
    return "\n".join(f"<code>d{i:03d}.example</code>: убран" for i in range(n)) + "\n  адреса в наборе: 3"


def test_the_cut_of_a_removal_is_counted_from_the_same_lines_the_screen_shows():
    """«➖» (del) — без сводки, каждая строка в перечне: число отрезанного и хвост
    «…и ещё N строк» на экране — одно и то же число."""
    out = _del_out(60)
    rows = tt.transit_result_rows(True, out, "del")
    assert len(rows) == 60, len(rows)
    budget = sum(len(r) + 1 for r in rows[:25])                 # ровно 25 строк
    cut = tt.transit_result_cut(True, out, budget=budget, cmd="del")
    assert cut == 35, cut
    text = tt.gateway_transit_result(True, out, budget=budget, cmd="del")
    assert text.split("\n")[-1] == "…и ещё 35 строк", text.split("\n")[-1]
    assert tt.transit_result_cut(True, out, budget=budget + 10 ** 5, cmd="del") == 0


def test_a_failed_run_or_empty_output_has_nothing_to_cut():
    """Скрипт отказал — на экране причина одной строкой, снимок не пишется."""
    assert tt.transit_result_cut(False, _del_out(300), budget=10, cmd="del") == 0
    assert tt.transit_result_cut(True, "", budget=10) == 0
    assert tt.transit_result_rows(True, "") == []
