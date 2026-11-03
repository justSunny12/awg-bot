"""Сторож длины карточки «🛟 Резерв» (bot/texts/reserve/section/card.py): худший
правдоподобный случай — десять голосующих, шлюзы без связи с резервом, все причины
и ошибки из состояния по 200 знаков, все блоки, что бывают разом (живость, приём с
пультом, Telegram второй стороны, «доделываю»), — не длиннее предела Telegram с
запасом. Причины режутся одним помощником (common._why), шлюзы без связи и
голосующие — хвостом «и ещё N» / « [+N]».

Цена ошибки: карточка длиннее 4096 — Telegram отвергает правку сообщения («message
is too long»), и карточка застывает в прежнем виде ровно тогда, когда админу нужно
видеть, что сломалось.
"""
from __future__ import annotations

import html
import re

import pytest

from awgbot.bot.texts.reserve.common import WHY_MAX
from awgbot.bot.texts.reserve.section.card import GW_DOWN_SHOWN, card_text
from awgbot.util.textfmt.atoms import TEXT_MAX, _u16, clip

pytestmark = pytest.mark.unit

BUDGET = 3900                                    # запас под предел 4096
T = "2026-11-02T14:05:00+03:00"
WHY = ("connection reset by peer: " * 10)[:200]  # ошибка из состояния — до 200 знаков
PEER = "Б" * 30


def _visible(text: str) -> int:
    """Видимая длина, как её считает Telegram: теги сняты, сущности раскрыты, UTF-16."""
    return _u16(html.unescape(re.sub(r"<[^>]+>", "", text)))


def _worst(**kw) -> dict:
    gw = [f"{'Г' * 20} {i:02d}" for i in range(9)]                       # свои шлюзы с агентом 4.2.0
    voters = [{"node": "b", "name": PEER, "w": "uns", "since": T, "silent": False}] + \
             [{"node": f"a{i}", "name": n, "w": "uns", "since": T, "silent": False} for i, n in enumerate(gw)]
    q = {"on": True, "why": [], "peer_name": PEER, "pending": True,
         "mine": {"v": 10, "t": 6, "leader_here": True, "y": 4, "role": "fenced", "fenced_since": T,
                  "voters": voters, "correlation": [([gw[0], gw[1]], "203.0.113.0/24"), ([gw[2], gw[3]], "198.51.100.0/24")]},
         "peer": {"active": True, "my_w": "uns", "my_since": T, "v": 10,
                  "gw_views": [{"node": f"b{i}", "name": f"шлюз {i} «{PEER}»", "w": "uns", "since": T} for i in range(5)],
                  "cand": {"own": "uns", "deadline_at": T}}}
    d = {"peer": {"name": PEER, "host": "h" * 64}, "peer_state": {"version": "4.2.0", "ready": True, "caps": []},
         "seen": "down", "down_since": T, "ready": True, "online": False, "mem_free": 100,
         "liveness_problem": "reserve.suspect_seconds 15 меньше keepalive линка пары 25 с плюс такт 5 с — "
                             "ложные «недоступен»; действует 30",
         "dir_out": (True, ""), "dir_in": (False, WHY), "out": "home", "inn": "serving",
         "pkg_sent_at": T, "pkg_error": WHY, "c9_wait": {"overdue": True}, "move_why": WHY, "out_relay_until": T,
         "gw_links": [{"name": n, "rsv1": True, "seen": "down", "since": T} for n in gw],
         "quorum": q, "rfpeer": {"offer_missing": WHY, "offer_missing_code": "down",
                                 "peer_use": {"since": T, "up": 10 ** 12, "dn": 10 ** 12}},
         "in_since": T, "peer_pult": True, "in_tg_wait": WHY, "in_keep": {"k": "relaunch", "why": WHY},
         "pkg_mem": {"size": "17,29 ГБ", "need": "40 ГБ"}, "await_merge": True, "code_open": True, "auto_block": WHY,
         "dns": {"name": "x" * 64, "current": "203.0.113.255", "provider_label": "DigitalPlat", "provider": "dp"},
         "traffic": {"out": "1 234,5 ГБ", "in": "1 234,5 ГБ", "month_label": "сентябрь"}, "ret_immediate": True}
    d.update(kw)
    return d


@pytest.mark.parametrize("kw", [
    {},                                                    # приём: пульт, Telegram второй стороны, «доделываю»
    {"inn": "idle"},                                       # «📥» на его сервере: голоса о нём, РФ, аварийный код
    {"inn": "idle", "code_open": False},                   # …вместо кода — «автоматический переезд невозможен»
    {"dir_out": (False, WHY)},                             # «📤» не готово — причина вместо данных переезда
], ids=["serving", "idle-code", "idle-auto", "out-not-ready"])
def test_the_worst_plausible_card_fits_telegram_with_a_margin(kw):
    text = card_text(_worst(**kw), "awg_bot")
    n = _visible(text)
    assert n <= BUDGET < TEXT_MAX, f"карточка {n} знаков (запас — {BUDGET}):\n{text}"


def test_reasons_from_state_are_clipped_by_one_helper():
    text = card_text(_worst(), "awg_bot")
    assert WHY not in text, "ошибка из состояния вошла в карточку целиком"
    keep = next(ln for ln in text.split("\n") if ln.startswith("└ ⚠️ интерфейс резерва не поднят: "))
    why = keep.removeprefix("└ ⚠️ интерфейс резерва не поднят: ").removesuffix(" — перезапускаю открытие раз в минуту")
    assert why.endswith("…") and _u16(why) == WHY_MAX, why


def test_a_clipped_reason_keeps_its_markup_whole():
    """Режется простой текст, экранируется потом: «&lt;» не разрывается посередине."""
    why = "x" * (WHY_MAX - 2) + "<b>&"
    text = card_text(_worst(pkg_error=why), "awg_bot")
    line = next(ln for ln in text.split("\n") if "не собираются" in ln)
    assert line.endswith("x" * (WHY_MAX - 2) + "&lt;…"), line


def test_short_reasons_are_untouched():
    text = card_text(_worst(move_why="«Beta» не на связи"), "awg_bot")
    assert "└ перевести кнопкой нельзя: «Beta» не на связи" in text, text


def test_gateways_out_of_touch_are_capped_with_a_count_line():
    lines = card_text(_worst(), "awg_bot").split("\n")
    named = [ln for ln in lines if ln.startswith("└ ⚠️ «Г") and " не на связи с " in ln]
    assert len(named) == GW_DOWN_SHOWN, named
    assert f"└ … и ещё {9 - GW_DOWN_SHOWN}" in lines, lines
    few = card_text(_worst(gw_links=_worst()["gw_links"][:GW_DOWN_SHOWN]), "awg_bot")
    assert "и ещё" not in few.split("📥")[0], few


@pytest.mark.parametrize("text,limit,out", [
    ("коротко", 10, "коротко"), ("ровно", 5, "ровно"), ("длиннее", 5, "длин…"), ("", 5, ""), (None, 5, ""),
    ("ab😀cd", 4, "ab…"),                                   # эмодзи — две единицы UTF-16: не разрезать пополам
])
def test_clip(text, limit, out):
    assert clip(text, limit) == out
