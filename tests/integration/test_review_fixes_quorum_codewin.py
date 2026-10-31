"""Правки по ревью окна аварийного кода и перезапуска бота резерва
(domain/services/reserve/codewin.py, quorum/candidate.py, liveness.py):

- окно, закрытое на время ввода кода (`pending`), такт кандидата не открывает снова,
  пока исход кода впереди; пользователи второй стороны у меня — окно закрыто
  (автоматический переезд — Т-РН3 «голосов хватило»), отметка `pending` после
  состоявшегося переезда снята — следующий эпизод окно откроет;
- попытки кода считаются за час, а не за окно: закрытие и новое окно их не обнуляют;
- после перезапуска бота свой голосующий «ещё не смотрел» — не повод закрыть окно кода
  (оно ждёт взгляда); живость линка пары, бывшая «недоступен», остаётся «недоступен» до
  первой жизни: Т-РУ8 и отметка `down_since` не повторяются.

Мир — окно кода вне решения `h4` (tests/integration/test_reserve_emergency_code.py) поверх
`box` (tests/integration/_reserve_vote_common.py). Хост и сеть не трогаются.

Цена ошибки: окно, открытое тактом посреди ввода кода, остаётся открытым всё
обслуживание — на экране роли «нужен код», после возврата ложное «переезд не нужен»;
три попытки в час обходятся закрытием и открытием окна; перезапуск бота резерва в
эпизоде — админу основы «переезд не нужен», через минуту снова «🆘 пришли код» со звуком,
и снова «недоступен» с новым моментом.
"""
from __future__ import annotations

import pytest

from awgbot.infra import rsvzone
from awgbot.util.quorum.voter import Voter

from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import LINE, PEER_ADMIN, ADMIN, box, notes
from tests.integration.test_reserve_emergency_code import CODE, h4

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["box", "h4"]

WRONG = {"step": "fail", "reason": "ключ пакета: SecretBox не снялся"}


def _window(s) -> dict:
    return dict(s.rsv_get("rsv_view").get("code") or {})


# ── ввод кода и такт кандидата (Q1-05) ──────────────────────────────────────

def test_a_tick_between_closing_the_window_and_taking_does_not_reopen_it(h4, monkeypatch):
    real = h4.s.reserve_zone_shell_ensure
    seen: list = []

    def interleave():
        seen.append(h4.cand.tick())                        # такт кворума в другом потоке — до taking
        return real()
    monkeypatch.setattr(h4.s, "reserve_zone_shell_ensure", interleave)
    assert h4.s.reserve_code_in(PEER_ADMIN, CODE)["ok"]
    assert seen, "подготовка: такт между закрытием окна и taking не случился"
    assert h4.s.reserve_in() == "taking"
    assert not h4.s.reserve_code_window()["open"], "такт посреди ввода кода открыл окно снова"
    monkeypatch.setattr(rsvzone, "open_status", lambda: dict(WRONG))
    acts = h4.s.reserve_in_tick()
    assert h4.s.reserve_code_window()["open"], "код не подошёл — окно не открыто снова"
    assert [n for n in notes(acts) if n.tg_id == PEER_ADMIN], "код не подошёл — админу основы ничего"


def test_while_hosting_after_the_code_the_window_stays_closed_and_a_new_episode_opens_it(h4, monkeypatch):
    assert h4.s.reserve_code_in(PEER_ADMIN, CODE)["ok"]
    h4.s.reserve_set_in("serving", kind="code")
    h4.advance(5)
    acts = h4.cand.tick()
    assert not h4.s.reserve_code_window()["open"] and not notes(acts)
    assert not _window(h4.s).get("pending"), "переезд по коду состоялся, а отметка ввода кода осталась"
    assert not h4.s.reserve_peer_admin_summary()["code_needed"], "в обслуживании экран роли просит код"
    h4.s.reserve_set_in("idle")
    h4.advance(5)
    h4.cand.own("see")
    acts = h4.cand.tick()
    assert not notes(acts), f"после возврата ложное «переезд не нужен»: {[n.text for n in notes(acts)]}"
    h4.advance(5)
    h4.cand.own("uns")
    h4.cand.tick()
    assert h4.s.reserve_code_window()["open"], "после переезда по коду новое окно не открывается"


def test_an_open_window_is_closed_when_an_automatic_move_starts(h4):
    """Окно открыто, а пользователи второй стороны уже едут ко мне автоматически (по точкам,
    по просьбе кворума): окно закрыто, админу основы — «голосов хватило, код не нужен»."""
    assert h4.s.reserve_code_window()["open"], "подготовка: окно не открыто"
    h4.s.reserve_set_in("taking", kind="dead", by="share")
    h4.advance(5)
    acts = h4.cand.tick()
    assert not h4.s.reserve_code_window()["open"], "в приёме окно кода осталось открытым"
    told = [n.text for n in notes(acts) if n.tg_id == PEER_ADMIN]
    assert told and "код не нужен" in told[0], f"админу основы не сказано, что код не нужен: {told}"


# ── попытки кода — на час (Q1-16) ───────────────────────────────────────────

def test_the_tries_survive_closing_and_reopening_the_window(h4, monkeypatch):
    real_status = rsvzone.open_status
    for _ in range(2):
        monkeypatch.setattr(rsvzone, "open_status", real_status)
        assert h4.s.reserve_code_in(PEER_ADMIN, CODE)["ok"]
        monkeypatch.setattr(rsvzone, "open_status", lambda: dict(WRONG))
        h4.s.reserve_in_tick()
    monkeypatch.setattr(rsvzone, "open_status", real_status)
    assert len(h4.s._code_tries(_window(h4.s))) == 2, "подготовка: две попытки не в счёте"
    h4.advance(5)
    h4.cand.own("see")
    h4.cand.tick()
    assert not h4.s.reserve_code_window()["open"], "подготовка: окно не закрылось своим «вижу»"
    assert len(h4.s._code_tries(_window(h4.s))) == 2, "закрытое окно потеряло попытки"
    h4.advance(5)
    h4.cand.own("uns")
    h4.cand.tick()
    assert h4.s.reserve_code_window()["open"], "подготовка: окно не открылось снова"
    assert len(h4.s._code_tries(_window(h4.s))) == 2, "новое окно обнулило попытки — три в час обходятся"
    res = h4.s.reserve_code_in(PEER_ADMIN, CODE)
    assert res["ok"], "третья попытка в час не принята"
    monkeypatch.setattr(rsvzone, "open_status", lambda: dict(WRONG))
    h4.s.reserve_in_tick()
    monkeypatch.setattr(rsvzone, "open_status", real_status)
    res = h4.s.reserve_code_in(PEER_ADMIN, CODE)
    assert not res["ok"] and res["why"] == "limit", f"четвёртая попытка в час: {res}"


# ── перезапуск бота резерва посреди эпизода (Q1-06) ─────────────────────────

def _restart(h4) -> Voter:
    """Перезапуск процесса резерва: память кандидата, голосующих, гостевого канала и живости — заново."""
    s = h4.s
    for k in ("_rsv_qcand", "_rsv_qvoters", "_rsv_gwviews", "_rsv_qgwviews", "_reserve_liveness", "_lv_base"):
        s.__dict__.pop(k, None)
    v = Voter(LINE, "a")
    v.epoch_in(7)
    s._qvoters[f"awgq{LINE}h"] = v                         # свежий голосующий: взгляд new
    h4.cand.v = v
    return v


def test_after_a_restart_the_window_waits_for_the_own_view(h4):
    before = h4.s.reserve_code_window()
    assert before["open"], "подготовка: окно не открыто"
    _restart(h4)
    h4.advance(5)
    acts = h4.cand.tick()
    assert h4.s.reserve_code_window()["open"], "перезапуск закрыл окно кода по «ещё не смотрел»"
    assert not notes(acts), f"перезапуск — админам: {[n.text[:60] for n in notes(acts)]}"
    h4.advance(45)
    h4.cand.own("uns")
    acts = h4.cand.tick()
    assert not notes(acts), f"тот же эпизод после перезапуска — снова «🆘 пришли код»: {[n.text[:60] for n in notes(acts)]}"
    assert h4.s.reserve_code_window()["since"] == before["since"], "эпизод окна сменился после перезапуска"


def test_after_a_restart_the_window_still_closes_on_a_real_see(h4):
    _restart(h4)
    h4.advance(5)
    h4.cand.tick()
    h4.advance(5)
    h4.cand.own("see")
    h4.cand.tick()
    assert not h4.s.reserve_code_window()["open"], "свой «вижу» после перезапуска не закрыл окно"


def test_after_a_restart_the_pair_link_stays_down_until_a_sign_of_life(box, monkeypatch):
    s = box.s
    s.rsv_update("rsv_peer_state", down_since="2026-10-21T03:10:00+03:00")
    s.__dict__.pop("_reserve_liveness", None)
    rx = {"v": 1000}
    monkeypatch.setattr(s, "_pair_rx", lambda: rx["v"])
    t = 50_000.0
    out = []
    for _ in range(30):                                    # 150 с тишины после старта
        t += 5
        out.append(s.reserve_liveness_tick(t))
    assert s.reserve_peer_seen() == "down", f"после перезапуска «недоступен» стало {s.reserve_peer_seen()!r}"
    assert not [n for _, ns in out for n in ns], "перезапуск в эпизоде — Т-РУ8 или «снова на связи» повторно"
    assert s.reserve_down_since() == "2026-10-21T03:10:00+03:00", "момент «недоступен» перезаписан"
    rx["v"] += 148                                         # keepalive второй стороны
    act, ns = s.reserve_liveness_tick(t + 5)
    assert s.reserve_peer_seen() == "ok" and ns and ns[0].tg_id == ADMIN, "жизнь после перезапуска не дала «снова на связи»"


def test_after_a_restart_without_an_episode_the_pair_link_starts_ok(box, monkeypatch):
    s = box.s
    s.rsv_update("rsv_peer_state", down_since="")
    s.__dict__.pop("_reserve_liveness", None)
    monkeypatch.setattr(s, "_pair_rx", lambda: 1000)
    assert s.reserve_peer_seen() == "ok"
    act, ns = s.reserve_liveness_tick(60_000.0)
    assert s.reserve_peer_seen() == "ok" and not ns
