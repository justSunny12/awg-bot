"""Тексты кворума — сквозные правила для всех уведомлений и ветки, которых нет в
test_quorum_texts*.py (domain/notices/reserve.py `quorum_*`, `gateway_tg_via`;
bot/texts/reserve/section.py — причины «голосование не включено»): без точки в
конце, имена экранированы и в «ёлочках» ровно один раз, лимит сообщения
Telegram при наибольшем составе, Т-К21 обеим сторонам, формы числа.

Цена ошибки: имя с `<` или `&` без экранирования — Telegram отвергает
сообщение целиком, и тревога не доходит; «шлюз 2 «Beta»» в двойных ёлочках —
текст читается как сломанный; сообщение длиннее 4096 знаков — не уходит вовсе;
Т-К21 с «бот написал ему», когда админ основы бота не открывал, — админ резерва
ждёт кода, которого никто не пришлёт.
"""
from __future__ import annotations

import pytest

from awgbot.bot.texts.reserve.section import quorum_mine_lines
from awgbot.domain.notices import reserve as rn
from awgbot.util import timeutil

pytestmark = pytest.mark.unit

SINCE = "2026-11-02T14:05:00+03:00"
CLOCK = timeutil.fmt_clock_ui(timeutil.parse_iso(SINCE))
INFO = {"hs": 21, "flow": True, "at": "2026-11-02T14:07:00+03:00"}
EVIL = "A<b>&amp;"
EVIL_ESC = "A&lt;b&gt;&amp;amp;"


def _all(name: str, peer: str, gws: list[str]) -> dict[str, str]:
    """Каждое уведомление кворума с этими именами."""
    return {
        "Т-К21 резерву, админ открывал": rn.quorum_code_needed_reserve(name, SINCE, 1, 3, 2, True),
        "Т-К21 резерву, не открывал": rn.quorum_code_needed_reserve(name, SINCE, 1, 3, 2, False),
        "Т-К21б основе": rn.quorum_code_needed_primary(name, peer, SINCE, 1, 3, 2),
        "Т-К12": rn.quorum_enabled(peer, gws, me=name),
        "Т-К12 без шлюзов": rn.quorum_enabled(peer, [], me=""),
        "Т-К12б": rn.quorum_enabled_peer(name),
        "Т-К7": rn.quorum_fenced(name, 1, 3, SINCE),
        "Т-К8": rn.quorum_unfenced(name, 2, 3),
        "Т-К5": rn.quorum_tie_leader(name, peer, SINCE, gws[:1], gws[1:], INFO),
        "Т-К5 без справки": rn.quorum_tie_leader(name, peer, SINCE, [], [], None),
        "Т-К5б написал": rn.quorum_tie_candidate(name, SINCE, gws[:1], gws[1:], INFO, gws[0] if gws else "", True),
        "Т-К5б некуда": rn.quorum_tie_candidate(name, SINCE, gws[:1], gws[1:], None, "", False),
        "Т-К5в": rn.quorum_tie_peer(name, SINCE, gws[:1], gws[1:], INFO, gws[0] if gws else ""),
        "Т-К6 остались": rn.quorum_tie_over(name, 2, 4, "stayed"),
        "Т-К6 переехали": rn.quorum_tie_over(name, 2, 4, "moved"),
        "Т-К6 переехали мои": rn.quorum_tie_over(name, 2, 4, "moved", mine=True),
        "Т-К6 без кворума": rn.quorum_tie_over(name, 2, 4, "fenced", mine=True),
        "Т-К24": rn.gateway_tg_via(name, peer, SINCE),
        "Т-К24 без имени резерва": rn.gateway_tg_via(name, "", SINCE),
    }


@pytest.mark.parametrize("key", list(_all("Alpha", "Beta", ["Pi5", "шлюз 2 «Beta»"])))
def test_no_quorum_notice_ends_with_a_period(key):
    text = _all("Alpha", "Beta", ["Pi5", "шлюз 2 «Beta»"])[key]
    for line in text.splitlines():
        assert not line.rstrip().endswith("."), f"{key}: строка с точкой в конце: {line!r}"
    assert ".." not in text and "  " not in text, f"{key}: двойная точка или двойной пробел: {text!r}"


@pytest.mark.parametrize("key", list(_all("Alpha", "Beta", ["Pi5"])))
def test_names_with_html_are_escaped_in_every_quorum_notice(key):
    text = _all(EVIL, EVIL, [EVIL, f"шлюз 2 «{EVIL}»"])[key]
    assert "<b>" not in text, f"{key}: имя не экранировано: {text!r}"
    assert EVIL_ESC in text, f"{key}: имени нет в тексте: {text!r}"


@pytest.mark.parametrize("key", [k for k in _all("A", "B", ["G"]) if k.startswith("Т-К5")])
def test_a_foreign_gateway_name_is_not_quoted_twice(key):
    text = _all("Beta", "Alpha", ["Pi5", "шлюз 2 «Beta»"])[key]
    names = [ln for ln in text.splitlines() if ln.startswith(("Видят:", "Не видят:"))]
    assert names, text
    for ln in names:                                    # подписи кнопок («🔀 Переезд на «Alpha»») — вложенные по правилу
        assert "««" not in ln and "»»" not in ln and "«шлюз" not in ln, f"{key}: двойные ёлочки: {ln!r}"
    if "Видят: «" in text:
        assert "«Pi5»" in text, f"{key}: своё имя шлюза без ёлочек: {text!r}"


def test_the_largest_tie_alarm_fits_one_telegram_message():
    gws = [("Ш" * 64) + str(i) for i in range(16)]
    for key, text in _all("A" * 64, "B" * 64, gws).items():
        assert len(text) < 4096, f"{key}: {len(text)} знаков — Telegram не примет"


def test_t_k21_tells_the_reserve_whether_the_primarys_admin_was_written_to():
    told = rn.quorum_code_needed_reserve("Alpha", SINCE, 1, 3, 2, True)
    not_told = rn.quorum_code_needed_reserve("Alpha", SINCE, 1, 3, 2, False)
    assert told.startswith(f"⚠️ «Alpha» не отвечает с {CLOCK}, а голосов для переезда не хватает: не видят 1 из 3, нужно 2")
    assert "бот написал ему" in told and "написать ему некуда" not in told
    assert "написать ему некуда" in not_told and "бот написал ему" not in not_told


def test_t_k21b_names_both_servers_and_the_numbers_to_the_primarys_admin():
    text = rn.quorum_code_needed_primary("Alpha", "Beta", SINCE, 2, 5, 3)
    assert text.startswith(f"🆘 «Beta» не видит «Alpha» с {CLOCK}"), text
    assert "не видят 2 из 5, нужно 3" in text and "пришли аварийный код" in text


def test_an_unknown_tie_outcome_reads_as_stayed():
    assert rn.quorum_tie_over("Alpha", 1, 2, "что-то") == rn.quorum_tie_over("Alpha", 1, 2, "stayed")


@pytest.mark.parametrize("hs,word", [(1, "устройство"), (2, "устройства"), (5, "устройств"), (21, "устройство"),
                                     (0, "устройств"), (11, "устройств")])
def test_the_traffic_note_says_the_device_count_in_the_right_form(hs, word):
    text = rn.quorum_tie_leader("Alpha", "Beta", SINCE, [], [], {"hs": hs, "flow": False, "at": ""})
    assert f"на связи {hs} {word}, трафика нет" in text, text


def test_the_traffic_note_names_who_said_it_and_when():
    text = rn.quorum_tie_candidate("Beta", SINCE, ["шлюз 1 «Beta»"], ["Alpha"], INFO, "шлюз 1 «Beta»", True)
    clock = timeutil.fmt_clock_ui(timeutil.parse_iso(INFO["at"]))
    assert f"трафик идёт (со слов шлюз 1 «Beta», {clock})" in text, text


# ── причины «голосование не включено» ───────────────────────────────────────

@pytest.mark.parametrize("why,want", [
    (["not_acked"], "состав голосующих ждёт подтверждения"),
    (["keys"], "ключи голосования ещё не у всех"),
    (["no_pair"], "голосующих нет"),
    ([], "голосующих нет"),
    (["слово-из-будущего"], "голосующих нет"),
])
def test_why_the_vote_is_off_names_the_rest_of_the_reasons(why, want):
    lines = quorum_mine_lines({"on": False, "why": why, "peer_name": "Beta"})
    assert lines == ["└ 🗳 Голосование не включено: " + want], lines


def test_the_first_known_reason_wins_over_the_later_ones():
    lines = quorum_mine_lines({"on": False, "why": ["keys", "not_acked"], "peer_name": "Beta"})
    assert lines == ["└ 🗳 Голосование не включено: ключи голосования ещё не у всех"], lines


def test_a_slot_reason_escapes_its_number():
    lines = quorum_mine_lines({"on": False, "why": ["slot:<9>"], "peer_name": "Beta"})
    assert "&lt;9&gt;" in lines[0] and "<9>" not in lines[0], lines


def test_an_enabled_vote_without_a_line_says_nothing():
    assert quorum_mine_lines({"on": True, "why": [], "peer_name": "Beta"}) == []
