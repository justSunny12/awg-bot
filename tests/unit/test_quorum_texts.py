"""Строки кворума резерва: блок голосов карточки «🛟 Резерв»
(bot/texts/reserve/section.py: Т-К1, Т-К2, Т-К3, Т-К3а, Т-К3в, Т-К20) и строка
панели агента шлюза (bot/texts/gateway/panel.py: Т-К17) — концепт «кворум»
§6.1, §6.5, §7.1, §7.4.

Цена ошибки: имя сервера или шлюза без экранирования — HTML-разметка
карточки ломается, и Telegram не показывает её вовсе; «видит» про молчащий
шлюз — админ верит голосу, которого нет; причина «не включено» не та —
админ ищет не там; строка панели при отсутствии раздела — шум у
админа шлюза без кворума.
"""
from __future__ import annotations

import pytest

from awgbot.bot.texts.gateway.panel import quorum_panel_line
from awgbot.util import timeutil
from awgbot.bot.texts.reserve.section import quorum_mine_lines, quorum_peer_lines

pytestmark = pytest.mark.unit

SINCE = "2026-11-02T14:05:00+03:00"
CLOCK = timeutil.fmt_clock_ui(timeutil.parse_iso(SINCE))   # как строки покажут время в поясе бота


def _card(**over) -> dict:
    q = {"on": True, "why": [], "side": "a", "peer_name": "Beta", "observe": False,
         "mine": {"g": 2, "v": 3, "t": 2, "leader_here": True, "y": 2, "role": "lead", "fenced_since": "",
                  "voters": [{"node": "b", "name": "Beta", "w": "see", "since": "", "age": 3, "silent": False},
                             {"node": "a1", "name": "Шлюз 1", "w": "see", "since": "", "age": 5, "silent": False},
                             {"node": "a2", "name": "Шлюз 2", "w": "uns", "since": SINCE, "age": 4, "silent": False}]}}
    q.update(over)
    return q


# ── карточка, блок «📤» ──────────────────────────────────────────────────────

def test_the_leader_line_says_how_many_see_it_and_the_threshold():
    lines = quorum_mine_lines(_card())
    assert lines[0] == "└ 🗳 Этот сервер видят 2 из 3 — пользователи переедут, если не увидят 2", lines
    assert lines[1] == f"└ «Beta» — видит · «Шлюз 1» — видит · «Шлюз 2» — не видит с {CLOCK}", lines


def test_a_silent_voter_is_never_shown_as_seeing():
    q = _card()
    q["mine"]["voters"][1].update(w="see", silent=True, age=120)
    row = quorum_mine_lines(q)[1]
    assert "«Шлюз 1» — молчит" in row, f"молчащий шлюз показан видящим: {row}"


def test_a_voter_never_heard_is_silent():
    q = _card()
    q["mine"]["voters"][1].update(w="", silent=True, age=-1)
    assert "«Шлюз 1» — молчит" in quorum_mine_lines(q)[1]


def test_names_are_escaped():
    q = _card(peer_name="<b>Beta</b>")
    q["mine"]["voters"][1]["name"] = "Шлюз <i>&"
    text = "\n".join(quorum_mine_lines(q))
    assert "<i>" not in text and "&lt;i&gt;&amp;" in text, text


def test_no_more_than_ten_voters_in_the_row():
    q = _card()
    q["mine"]["voters"] = [{"node": f"a{i}", "name": f"Шлюз {i}", "w": "see", "since": "", "age": 1, "silent": False}
                           for i in range(1, 15)]
    assert quorum_mine_lines(q)[1].count("— видит") == 10


def test_a_fenced_leader_line_names_the_episode_start():
    q = _card()
    q["mine"].update(role="fenced", y=1, fenced_since=SINCE)
    line = quorum_mine_lines(q)[0]
    assert line.startswith(f"└ ⚠️ Без кворума с {CLOCK} — этот сервер видят 1 из 3"), line


def test_in_observation_only_a_fenced_leader_is_not_said_to_stop_answering():
    """Скрытый ключ act = false — решений нет: «бот не отвечает пользователям»
    было бы неправдой."""
    q = _card(observe=True)
    q["mine"].update(role="fenced", y=1)
    line = quorum_mine_lines(q)[0]
    assert "не отвечает" not in line and line.startswith("└ 🗳 Этот сервер видят 1 из 3"), line


def test_the_bot_on_the_reserve_line_comes_from_its_count():
    q = _card()
    q["mine"].update(leader_here=False, y=3, from_guest=True)
    assert quorum_mine_lines(q)[0] == "└ 🗳 Бот на «Beta» видят 3 из 3"


def test_without_a_count_the_line_says_voters_and_threshold():
    q = _card()
    for k in ("y", "role"):
        q["mine"].pop(k)
    assert quorum_mine_lines(q)[0].startswith("└ 🗳 Голосующих о"), quorum_mine_lines(q)


@pytest.mark.parametrize("code, want", [
    ("peer_old", "у «Beta» нет v4.2.0 — переезды по прежним правилам"),
    ("no_gateways", "у этого сервера нет шлюзов с агентом v4.2.0 — переезд по прежним правилам: по взглядам шлюзов, кнопкой и по аварийному коду"),
    ("timing:up_seconds 50 < …", "цифры времени не сходятся (reserve.quorum в app.yaml)"),
    ("overlap:169.254.0.0/16", "блок зондов <code>169.254.88.0/24</code> пересекается с <code>169.254.0.0/16</code>"),
    ("slot:61", "«Шлюз 61»: номер слота больше 58 — в голосовании не участвует"),
])
def test_why_the_vote_is_off_is_said_by_the_first_reason(code, want):
    lines = quorum_mine_lines(_card(on=False, why=[code, "no_gateways"]))
    assert lines == [f"└ 🗳 Голосование не включено: {want}"], lines


def test_an_overlap_subnet_is_escaped():
    line = quorum_mine_lines(_card(on=False, why=["overlap:<x>"]))[0]
    assert "<x>" not in line and "&lt;x&gt;" in line


def test_no_quorum_data_means_no_lines():
    assert quorum_mine_lines({}) == [] and quorum_peer_lines({}) == []
    assert quorum_mine_lines(_card(mine={})) == []


# ── карточка, блок «📥» ──────────────────────────────────────────────────────

def _peer(**over) -> dict:
    p = {"active": True, "why": [], "v": 2, "my_w": "see", "my_since": "", "y": 2, "yv": 2, "r": "lead",
         "gw_views": [{"node": "b1", "name": "шлюз 1 «Beta»", "w": "see", "since": ""}]}
    p.update(over)
    return {"peer_name": "Beta", "peer": p}


def test_i_see_the_peer_and_so_do_its_gateways():
    assert quorum_peer_lines(_peer()) == ["└ 🗳 «Beta» вижу я и шлюз 1 «Beta» — 2 из 2"]


def test_only_i_do_not_see_the_peer_points_at_my_own_network():
    lines = quorum_peer_lines(_peer(my_w="uns", my_since=SINCE))
    assert lines == [f"└ 🟡 Этот сервер не видит «Beta» с {CLOCK}, а его шлюзы видят — похоже, дело в сети этого сервера"]


def test_many_do_not_see_the_peer():
    q = _peer(my_w="uns", my_since=SINCE, v=3, gw_views=[{"node": "b1", "name": "x", "w": "uns", "since": ""}])
    assert quorum_peer_lines(q) == [f"└ 🟡 «Beta» не видят 2 из 3 с {CLOCK}"]


def test_a_peer_line_without_the_vote_has_no_lines():
    assert quorum_peer_lines(_peer(active=False)) == []
    assert quorum_peer_lines(_peer(my_w="new")) == []


# ── панель агента, Т-К17 ─────────────────────────────────────────────────────

def test_the_panel_line_when_the_gateway_sees_its_server():
    assert quorum_panel_line({"name": "Alpha", "w": "see", "y": 2, "v": 3}) == "🗳 Голосую о «Alpha»: вижу · видят 2 из 3"
    assert quorum_panel_line({"name": "Alpha", "w": "held"}) == "🗳 Голосую о «Alpha»: вижу"


def test_the_panel_line_when_it_does_not_see_and_the_vote_reached_the_reserve():
    line = quorum_panel_line({"name": "Alpha", "w": "uns", "since": SINCE, "reserve_online": True})
    assert line.startswith(f"🗳 Голосую о «Alpha»: не вижу с {CLOCK} — голос у "), line


def test_the_panel_line_when_the_reserve_is_offline_too():
    line = quorum_panel_line({"name": "Alpha", "w": "uns", "since": SINCE, "reserve_online": False})
    assert line.startswith(f"🗳 Голосую о «Alpha»: не вижу с {CLOCK} — ") and line.endswith("не на связи, голос не дошёл"), line


def test_a_missed_reply_is_not_yet_a_vote():
    line = quorum_panel_line({"name": "Alpha", "w": "sus", "since": SINCE, "reserve_online": False})
    assert line == f"🗳 Голосую о «Alpha»: не вижу с {CLOCK}", "«голос не дошёл» до того, как голос «нет» вообще есть"


def test_the_panel_line_before_any_reply():
    assert quorum_panel_line({"name": "Alpha", "w": "new"}) == "🗳 Голосую о «Alpha»: ещё нет ответа"


def test_the_panel_line_escapes_the_server_name_and_is_empty_without_the_section():
    assert "<b>" not in quorum_panel_line({"name": "<b>A</b>", "w": "see"})
    assert quorum_panel_line({}) == ""
    assert quorum_panel_line({"w": "see"}) == "🗳 Голосую о «сервер AWG»: вижу"


def test_the_panel_shows_the_line_from_the_status():
    """Строка Т-К17 доезжает в панель из поля quo статуса (его кладёт монитор агента)."""
    from awgbot.bot.texts.gateway import panel
    from awgbot.domain.gateway.base import GwStatus
    st = GwStatus()
    st.quo = {"name": "Alpha", "w": "see", "y": 2, "v": 3}
    assert "🗳 Голосую о «Alpha»: вижу · видят 2 из 3" in panel.gateway_panel(st), "строка кворума не попала в панель"
    st.quo = {}
    assert "🗳" not in panel.gateway_panel(st), "строка кворума без раздела"
