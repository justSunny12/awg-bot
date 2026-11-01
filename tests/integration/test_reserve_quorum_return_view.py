"""Свой взгляд резерва на AWG второй стороны после возврата её пользователей
(domain/services/reserve/quorum/candidate.py `_quorum_after_return`,
util/quorum/voter.py `Voter.restart`).

Стенд (s06_return_tail): основа отрезана, резерв открылся по коду и голосующий
резерва честно сказал «не вижу»; связь вернулась, основа вернула пользователей
кнопкой. Пока её пользователи у резерва, кандидата нет; на возврате (relay —
зона свободна) кандидат вставал с тем же «не вижу», набранным до возврата, и
при v = 1 окно кода открывалось через пару секунд после возврата при живой
основе — до двух ответов подряд (до ~40 с при пробах раз в 15 с ± 40 %).

Мир — `_Box`/`Cand` из _reserve_vote_common (модельные часы, пакет у резерва,
линия «Beta»); голосующий — настоящая машина `Voter` с пробами по 15 с; сервер
не отвечает до `answer_at`, после — отвечает каждой пробе.

Цена ошибки: окно кода и Т-К21 обоим админам сразу после каждого возврата,
которому предшествовало «не вижу» (разрез, авария с возвратом), — ложная авария
при живой основе и код в окно, которое закроется ответом пробы; в другую
сторону — основа, умершая после возврата, не должна остаться без окна: «не
вижу» — через no_s от возврата.
"""
from __future__ import annotations

import types

import pytest

from awgbot.domain.services.reserve.quorum import voter as votermod
from awgbot.util.quorum.probe import Probe, Reply
from awgbot.util.quorum.voter import Voter
from tests.integration import _reserve_common as rc
from tests.integration._reserve_vote_common import LINE, PEER_ADMIN, Cand, box, notes

pytestmark = pytest.mark.integration
rsv = rc.rsv
__all__ = ["box"]

SOLO = ["b", "a"]                                     # шлюзов у «Beta» нет: голосую только я (v = 1)
NAME = f"awgq{LINE}h"


class _World:
    """Голосующий видел сервер, затем — разрез с `cut_at` (ответов нет), ответы снова с
    `answer_at` (None — больше никогда). Такт кандидата — раз в 5 с."""

    def __init__(self, box, monkeypatch, *, cut_at: float, answer_at: float | None):
        monkeypatch.setattr(votermod, "time", types.SimpleNamespace(monotonic=box.clock.monotonic, time=box.clock.time))
        box.peer_admin_opened()
        self.b, self.c = box, Cand(box, members=SOLO)
        self.t0 = box.clock.t
        self.v = Voter(LINE, "a")
        self.v.start(self.t0)
        box.s._qvoters[NAME] = self.v
        self.c.v = self.v
        self.cut_at, self.answer_at = self.t0 + cut_at, (None if answer_at is None else self.t0 + answer_at)
        self.opened: list[float] = []
        self.acts: list[tuple] = []

    def answers(self, now: float) -> bool:
        if now < self.cut_at:
            return True
        return self.answer_at is not None and now >= self.answer_at

    def run(self, seconds: float) -> None:
        s, v = self.b.s, self.v
        for _ in range(int(seconds)):
            now = self.b.clock.t
            for a in v.tick(now):
                if a != "probe":
                    continue
                p = Probe(LINE, 1, "a", int(v.epoch), v.w_for_probe(), v.probe_out(now, 15))
                if self.answers(now):
                    v.reply_in(Reply(LINE, 1, 7, "lead", p.s, 0, 1), now + 0.2)
            if int(now - self.t0) % 5 == 0:
                self.acts += self.c.tick() + s.reserve_code_link_watch()
                if s.reserve_code_window()["open"] and not self.opened:
                    self.opened.append(now - self.t0)
            self.b.advance(1)


def _host_then_return(w: _World, box, *, serving_s: int, via_releasing: bool = True) -> None:
    """Пользователи основы у меня `serving_s` с, затем возврат: releasing (такт его видит) → relay."""
    box.s.reserve_set_in("serving")
    w.run(serving_s)
    if via_releasing:
        box.s.reserve_set_in("releasing")
        w.run(5)
    box.s.reserve_set_in("relay")


def test_no_code_window_right_after_the_return_from_a_stale_dont_see(box, monkeypatch):
    """Ход стенда: видел → разрез (через 30 с) → «не вижу» за время обслуживания → связь
    вернулась на 140-й секунде, возврат на 145-й. Окна кода нет ни на возврате, ни после;
    Т-К21 никому; взгляд снова «вижу»."""
    w = _World(box, monkeypatch, cut_at=30, answer_at=140)
    _host_then_return(w, box, serving_s=140)
    assert w.v.view in ("uns", "new"), f"подготовка: к возврату взгляд «не вижу» не сложился ({w.v.view})"
    w.run(120)
    assert not w.opened, f"окно кода при живой основе через {w.opened} с: {[n.text for n in notes(w.acts)]}"
    assert not notes(w.acts), [n.text for n in notes(w.acts)]
    assert w.v.view == "see"


def test_the_same_when_the_tick_did_not_see_releasing(box, monkeypatch):
    """Такт кандидата между serving и relay не попал на releasing (он короток): возврат —
    по пересылке хвоста, то же."""
    w = _World(box, monkeypatch, cut_at=30, answer_at=140)
    _host_then_return(w, box, serving_s=140, via_releasing=False)
    w.run(120)
    assert not w.opened, f"окно кода при живой основе через {w.opened} с"
    assert w.v.view == "see"


def test_a_primary_dead_after_the_return_still_gets_the_window_no_s_later(box, monkeypatch):
    """Честный путь: основа вернула пользователей и сразу умерла (ответов больше нет) — окно
    кода открыто и Т-К21 админу основы, не позже no_s от возврата и такта кандидата."""
    w = _World(box, monkeypatch, cut_at=30, answer_at=None)
    _host_then_return(w, box, serving_s=140)
    t_back = box.clock.t - w.t0
    w.run(200)
    no_s = w.v.timing.no_s
    assert w.opened, "основа умерла после возврата: окно кода не открылось — пользователи без VPN"
    assert w.opened[0] <= t_back + no_s + 5 + 1, f"окно через {w.opened[0]} с — позже no_s от возврата ({t_back} с)"
    assert {n.tg_id for n in notes(w.acts)} >= {PEER_ADMIN}, [n.text for n in notes(w.acts)]


def test_serving_ended_without_a_return_keeps_the_view(box, monkeypatch):
    """Обслуживание кончилось не возвратом (serving → idle: срыв, снятая зона) — живость
    второй стороны этим не доказана: «не вижу» остаётся, окно — как прежде."""
    w = _World(box, monkeypatch, cut_at=30, answer_at=None)
    box.s.reserve_set_in("serving")
    w.run(140)
    box.s.reserve_set_in("idle")
    w.run(10)
    assert w.v.view == "uns"
    assert w.opened, "без возврата прежнее «не вижу» — окно кода по-прежнему"


def test_restart_resets_only_a_not_seeing_view():
    """Voter.restart: «вижу» не трогает; иначе — new, молчание от момента сброса, эпоха и
    обязательство на месте."""
    v = Voter(LINE, "a")
    v.start(0.0)
    v.view = "see"
    assert v.restart(10.0) is False and v.view == "see"
    v.view, v.last_reply, v.epoch = "uns", 5.0, 8
    v.said_no(6.0)
    assert v.restart(100.0) is True
    assert (v.view, v.started, v.last_reply, v.epoch) == ("new", 100.0, 0.0, 8)
    assert v.pledge is not None
    assert "uns" not in v.tick(100.0 + v.timing.no_s - 1)
    assert "uns" in v.tick(100.0 + v.timing.no_s)
