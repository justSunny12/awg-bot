"""Строки и уведомления кворума этапа ничьей и возвратов (bot/texts/reserve/section.py,
peeradmin.py, guest.py; domain/notices/reserve.py; концепт «кворум» §6.1, §6.4,
§6.6, §7.1–7.3): Т-К3, Т-К3б, Т-К4, Т-К15 (карточка), Т-К18 (диалог переезда),
Т-К19 (диалог С9), Т-К22 (экран роли), Т-К1 (карточка гостя), Т-К5…Т-К6.

Цена ошибки: имя без экранирования — Telegram отвергает сообщение целиком, и
тревога ничьей не доходит вовсе; срок переезда не назван — админ не знает,
сколько у него времени; «бот написал ему», когда писать некуда, — админ
кандидата ждёт решения, которого никто не примет; точка в конце уведомления —
против правила текстов.
"""
from __future__ import annotations

import pytest

from awgbot.bot.texts.reserve import guest as guest_txt
from awgbot.bot.texts.reserve import peeradmin as pa_txt
from awgbot.bot.texts.reserve.section import force_dialog, move_dialog, quorum_mine_lines, quorum_peer_lines
from awgbot.domain.notices import reserve as rn
from awgbot.util import timeutil

pytestmark = pytest.mark.unit

SINCE = "2026-11-02T14:05:00+03:00"
UNTIL = "2026-11-02T14:07:00+03:00"
CLOCK = timeutil.fmt_clock_ui(timeutil.parse_iso(SINCE))
UNTIL_CLOCK = timeutil.fmt_clock_ui(timeutil.parse_iso(UNTIL))


def _peer(**p) -> dict:
    base = {"active": True, "why": [], "v": 3, "my_w": "uns", "my_since": SINCE, "y": 0, "yv": 0, "r": "",
            "gw_views": [{"node": "b1", "name": "шлюз 1 «Beta»", "w": "uns", "since": SINCE},
                         {"node": "b2", "name": "шлюз 2 «Beta»", "w": "see", "since": ""}],
            "cand": {"no": 2, "see": 1, "v": 3, "own": "uns", "deadline_at": UNTIL, "stage": "", "tie": False}}
    base.update(p)
    return {"on": True, "why": [], "side": "a", "peer_name": "Beta", "observe": False, "peer": base}


def _mine(**m) -> dict:
    base = {"g": 2, "v": 4, "t": 3, "leader_here": True, "y": 2, "role": "lead", "fenced_since": "", "voters": []}
    base.update(m)
    return {"on": True, "why": [], "side": "a", "peer_name": "Beta", "observe": False, "mine": base}


# ── карточка: блок «📥» ──────────────────────────────────────────────────────

def test_t_k3_names_when_the_users_will_move_here():
    lines = quorum_peer_lines(_peer())
    assert lines == [f"└ 🟡 «Beta» не видят 2 из 3 с {CLOCK} — если так останется до {UNTIL_CLOCK}, "
                     "пользователи переедут сюда"], lines


def test_t_k3_without_a_deadline_promises_nothing():
    q = _peer()
    q["peer"]["cand"]["deadline_at"] = ""
    line = quorum_peer_lines(q)[0]
    assert "переедут" not in line, f"срока нет, а строка обещает переезд: {line}"


def test_t_k3b_case_3_gateways_do_not_see_but_this_server_does():
    """Концепт «кворум» §7.1 Т-К3б: шлюзы «Beta» не видят её, а этот сервер видит —
    случай 3, переезд через T_up_rf; админу нужен срок."""
    q = _peer(my_w="see", y=1, yv=3, gw_views=[{"node": "b1", "name": "шлюз 1 «Beta»", "w": "uns", "since": SINCE},
                                                 {"node": "b2", "name": "шлюз 2 «Beta»", "w": "uns", "since": SINCE}])
    q["peer"]["cand"].update(own="see", no=2, deadline_at=UNTIL)
    lines = quorum_peer_lines(q)
    assert lines == [f"└ 🟡 Шлюзы «Beta» не видят его с {CLOCK}, а этот сервер видит — если так останется до "
                     f"{UNTIL_CLOCK}, пользователи переедут сюда"], f"нет строки Т-К3б: {lines}"


def test_t_k3a_all_good_counts_who_sees():
    q = _peer(my_w="see", y=3, yv=3, gw_views=[{"node": "b1", "name": "шлюз 1 «Beta»", "w": "see", "since": ""}])
    q["peer"]["cand"].update(own="see", no=0, deadline_at="")
    line = quorum_peer_lines(q)[0]
    assert line.startswith("└ 🗳 «Beta» вижу я") and line.endswith("3 из 3"), line


def test_t_k4_is_also_shown_for_a_tie_about_the_other_server():
    """Концепт «кворум» §6.1: ничья (Т-К4) — состояние и блока «📥» (я —
    кандидат): у кандидата тоже нет решения, и админ должен видеть почему."""
    q = _peer(gw_views=[{"node": "b1", "name": "шлюз 1 «Beta»", "w": "uns", "since": SINCE},
                        {"node": "b2", "name": "шлюз 2 «Beta»", "w": "see", "since": ""},
                        {"node": "b3", "name": "шлюз 3 «Beta»", "w": "see", "since": ""}], v=4)
    q["peer"]["cand"].update(no=2, see=2, v=4, deadline_at="", tie=True)
    text = "\n".join(quorum_peer_lines(q))
    assert "Ничья" in text, f"ничья о втором сервере не показана: {text}"


def test_peer_lines_escape_names():
    q = _peer()
    q["peer_name"] = "<b>Beta</b>"
    text = "\n".join(quorum_peer_lines(q))
    assert "<b>" not in text and "&lt;b&gt;" in text, text


# ── карточка: блок «📤» ──────────────────────────────────────────────────────

def test_t_k4_tie_line_on_the_leader():
    lines = quorum_mine_lines(_mine(tie_since=SINCE))
    assert lines[0] == f"└ ⚠️ Ничья с {CLOCK}: этот сервер видят 2, не видят 2 — сам бот пользователей не переведёт", lines


def test_t_k4_gives_way_to_the_fenced_line():
    lines = quorum_mine_lines(_mine(tie_since=SINCE, role="fenced", fenced_since=SINCE, y=1))
    assert lines[0].startswith("└ ⚠️ Без кворума"), lines


@pytest.mark.parametrize("names,tail", [(["Beta", "Шлюз 1"], "оба голоса"), (["Шлюз 1", "Шлюз 2", "Шлюз 3"], "все их голоса")])
def test_t_k15_correlation_names_the_net(names, tail):
    lines = quorum_mine_lines(_mine(correlation=[[names, "203.0.113.0/24"]]))
    line = [x for x in lines if "из одной сети" in x]
    assert len(line) == 1, lines
    assert " и ".join(f"«{n}»" for n in names) in line[0] and "<code>203.0.113.0/24</code>" in line[0], line[0]
    assert line[0].endswith(tail), line[0]


def test_t_k15_absent_without_a_shared_net():
    assert not [x for x in quorum_mine_lines(_mine(correlation=[])) if "одной сети" in x]


def test_t_k15_escapes_names():
    text = "\n".join(quorum_mine_lines(_mine(correlation=[[["<i>x", "y&"], "203.0.113.0/24"]])))
    assert "<i>" not in text and "&lt;i&gt;x" in text and "y&amp;" in text, text


# ── диалоги ─────────────────────────────────────────────────────────────────

_MOVE = {"name": "Beta", "record": "vpn-a.example.org", "target": "198.51.100.7", "manual": False, "own": "198.51.100.10"}


def test_t_k18_in_the_move_dialog_only_with_the_count():
    assert "Этот сервер сейчас видят" not in move_dialog(_MOVE)
    text = move_dialog({**_MOVE, "quorum": {"y": 1, "v": 3}})
    assert text.endswith("\n\nЭтот сервер сейчас видят 1 из 3."), text[-120:]


_FORCE = {"name": "Beta", "since": SINCE, "record": "vpn-a.example.org", "own": "198.51.100.10", "manual": False, "jrn": True}


def test_t_k19_three_forms():
    assert "Бот на" not in force_dialog({**_FORCE, "quorum": {}}), "без кворума строка Т-К19 есть"
    seen = force_dialog({**_FORCE, "quorum": {"y": 2, "no": 1, "v": 3, "known": True}})
    assert seen.endswith("\nБот на «Beta» видят 2 из 3 — если он жив, правки, сделанные там после этой минуты, "
                         "придут хвостом."), seen[-160:]
    nobody = force_dialog({**_FORCE, "quorum": {"y": 0, "no": 3, "v": 3, "known": True}})
    assert nobody.endswith("\nБот на «Beta» не видит никто."), nobody[-80:]


# ── экран роли и карточка гостя ─────────────────────────────────────────────

_PA = {"me": "Alpha", "peer": {"name": "Beta"}, "version": "4.2.0", "direction_in": (True, ""), "my_ready": True,
       "awg_ok": True, "link_online": True, "seen": "ok"}


def test_t_k22_on_the_peer_admin_screen():
    assert "🗳" not in pa_txt.screen(_PA), "без кворума строка Т-К22 есть"
    text = pa_txt.screen({**_PA, "quorum": {"y": 3, "v": 4, "w": "see", "since": ""}})
    assert "🗳 Твой «Beta» видят 3 из 4 · этот сервер — видит" in text, text
    text = pa_txt.screen({**_PA, "quorum": {"y": 1, "v": 4, "w": "uns", "since": SINCE}})
    assert f"🗳 Твой «Beta» видят 1 из 4 · этот сервер — не видит с {CLOCK}" in text, text


def test_t_k22_escapes_the_name():
    text = pa_txt.screen({**_PA, "peer": {"name": "<b>B"}, "quorum": {"y": 1, "v": 2, "w": "see"}})
    assert "<b>B" not in text and "«&lt;b&gt;B»" in text


_GUEST = {"name": "Beta", "since": SINCE, "own_name": "Alpha", "primary": {"status": "dead", "since": SINCE}}


def test_t_k1_on_the_guest_card():
    assert "🗳" not in guest_txt.card_text(_GUEST)
    text = guest_txt.card_text({**_GUEST, "quorum": {"y": 3, "v": 3, "t": 2}})
    assert "🗳 Бот здесь видят 3 из 3" in text, text


# ── уведомления Т-К5…Т-К6 ───────────────────────────────────────────────────

INFO = {"hs": 37, "flow": True, "at": "2026-11-02T14:07:00+03:00"}


def _all_tie_texts() -> list[str]:
    return [rn.quorum_tie_leader("Alpha", "Beta", SINCE, ["Beta", "Шлюз 3"], ["Шлюз 1", "Шлюз 2"], INFO),
            rn.quorum_tie_candidate("Alpha", SINCE, ["шлюз 3 «Alpha»"], ["Beta", "шлюз 1 «Alpha»"], INFO, "шлюз 3 «Alpha»", True),
            rn.quorum_tie_candidate("Alpha", SINCE, ["шлюз 3 «Alpha»"], ["Beta"], None, "", False),
            rn.quorum_tie_peer("Alpha", SINCE, ["шлюз 3 «Alpha»"], ["Beta"], INFO, "шлюз 3 «Alpha»"),
            rn.quorum_tie_over("Alpha", 3, 4, "stayed"), rn.quorum_tie_over("Alpha", 2, 4, "moved"),
            rn.quorum_tie_over("Alpha", 1, 4, "fenced", mine=True)]


def test_tie_notices_end_without_a_period():
    for text in _all_tie_texts():
        assert not text.rstrip().endswith("."), f"уведомление с точкой в конце: …{text[-60:]}"


def test_t_k5_names_both_sides_and_its_own_traffic():
    text = rn.quorum_tie_leader("Alpha", "Beta", SINCE, ["Beta", "Шлюз 3"], ["Шлюз 1", "Шлюз 2"], INFO)
    assert text.startswith(f"🚨 Ничья о сервере «Alpha» с {CLOCK}"), text
    assert "Видят: «Beta», «Шлюз 3»" in text and "Не видят: «Шлюз 1», «Шлюз 2»" in text, text
    assert "На «Alpha»: на связи 37 устройств, трафик идёт" in text, text
    assert "«🔀 Переезд на «Beta»»" in text


def test_t_k5b_says_whether_the_peer_admin_was_told():
    told = rn.quorum_tie_candidate("Alpha", SINCE, ["x"], ["y"], INFO, "шлюз 3 «Alpha»", True)
    assert "бот написал ему" in told and "некуда" not in told, told
    not_told = rn.quorum_tie_candidate("Alpha", SINCE, ["x"], ["y"], INFO, "шлюз 3 «Alpha»", False)
    assert "написать ему некуда" in not_told and "бот написал ему" not in not_told, not_told


def test_t_k5b_traffic_note_says_whose_words_or_that_there_is_none():
    text = rn.quorum_tie_candidate("Alpha", SINCE, ["x"], ["y"], INFO, "шлюз 3 «Alpha»", True)
    assert "на связи 37 устройств, трафик идёт (со слов" in text and "шлюз 3" in text, text
    none = rn.quorum_tie_candidate("Alpha", SINCE, ["x"], ["y"], None, "", True)
    assert "Справки о трафике «Alpha» нет: видящие не на связи с этим сервером" in none, none


def test_t_k5b_gateway_names_of_the_other_side_are_not_quoted_twice():
    """Концепт «кворум» §7.3: «Видят: шлюз 3 «Alpha»» — имя шлюза второй стороны
    уже несёт кавычки сервера; ещё одни вокруг — «шлюз 3 «Alpha»»."""
    text = rn.quorum_tie_candidate("Alpha", SINCE, ["шлюз 3 «Alpha»"], ["шлюз 1 «Alpha»"], INFO, "шлюз 3 «Alpha»", True)
    assert "«шлюз" not in text, f"двойные кавычки у имени шлюза: {text}"


def test_t_k5v_tells_the_peer_admin_about_the_code():
    text = rn.quorum_tie_peer("Alpha", SINCE, ["x"], ["y"], INFO, "s")
    assert text.startswith(f"🚨 Ничья о твоём сервере «Alpha» с {CLOCK}"), text
    assert text.endswith("пришли аварийный код"), text


def test_tie_notices_escape_names():
    for text in (rn.quorum_tie_leader("<b>A", "B&", SINCE, ["<i>"], ["x"], INFO),
                 rn.quorum_tie_candidate("<b>A", SINCE, ["<i>"], ["x"], INFO, "<u>", True),
                 rn.quorum_tie_peer("<b>A", SINCE, ["<i>"], ["x"], INFO, "<u>"),
                 rn.quorum_tie_over("<b>A", 1, 2, "stayed")):
        assert "<b>" not in text and "<i>" not in text and "<u>" not in text, text
        assert "&lt;b&gt;A" in text


@pytest.mark.parametrize("outcome,mine,tail", [("stayed", False, "пользователи остались на «Alpha»"),
                                               ("moved", False, "пользователи переехали сюда"),
                                               ("fenced", True, "«Alpha» без кворума, бот не принимает решений")])
def test_t_k6_says_how_it_ended(outcome, mine, tail):
    text = rn.quorum_tie_over("Alpha", 3, 4, outcome, mine=mine)
    assert text == f"✅ Ничьей о «Alpha» больше нет: видят 3 из 4 — {tail}", text
