"""Окно аварийного кода и свежий зонд резерва к AWG второй стороны
(domain/services/reserve/quorum/voter.py `reserve_quorum_probe_unsent`,
quorum/candidate.py, util/quorum/voter.py).

Стенд (s12_relay_flush): конфиг зонда `qprobe` пришёл, такт поднял интерфейс и
пространство, а сокет зонда runtime создать не успел — повтор через 30 с; пробы
этих 30 с уходили в пустоту, голосующий считал `no_s` от своего создания и через
45 с сказал «не вижу» о живой основе. Пока у резерва её пользователи (переезд
кнопкой), кандидата нет; после возврата (relay — зона свободна) кандидат встал
со своим «не вижу», и при v = 1 окно кода открылось на полминуты при живой
основе — до двух ответов подряд.

Мир — `_Box`/`Cand` из _reserve_vote_common (модельные часы, пакет у резерва,
линия «Beta»); голосующий — настоящая машина `Voter` с пробами по 15 с,
неушедшие — тем же вызовом, что делает runtime (`quorum_probe_unsent`).

Цена ошибки: окно кода и Т-К21 обоим админам при живой основе после каждого
свежего зонда (пара, новый конфиг, старт) — ложная авария, и код, введённый в
окно, которое закроется ответом пробы; в другую сторону — слепота, засчитанная
молчанием мёртвой основы, не должна пропасть: сокет встал, ответов нет —
окно открывается через `no_s`.
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
    """Свежий зонд: голосующий создан сейчас, сокет встаёт через `sock_in` с, сервер
    отвечает с `answer_in` с (None — мёртв). Такт кандидата — раз в 5 с."""

    def __init__(self, box, monkeypatch, *, sock_in: float, answer_in: float | None):
        monkeypatch.setattr(votermod, "time", types.SimpleNamespace(monotonic=box.clock.monotonic, time=box.clock.time))
        box.peer_admin_opened()
        self.b, self.c = box, Cand(box, members=SOLO)
        self.t0 = box.clock.t
        self.v = Voter(LINE, "a")
        self.v.start(self.t0)
        box.s._qvoters[NAME] = self.v
        self.c.v = self.v
        self.sock_at = self.t0 + sock_in
        self.answer_at = None if answer_in is None else self.t0 + answer_in
        self.opened: list[float] = []
        self.acts: list[tuple] = []

    def run(self, seconds: float) -> None:
        s, v = self.b.s, self.v
        for _ in range(int(seconds)):
            now = self.b.clock.t
            for a in v.tick(now):
                if a != "probe":
                    continue
                p = Probe(LINE, 1, "a", int(v.epoch), v.w_for_probe(), v.probe_out(now, 15))
                if now < self.sock_at:
                    s.quorum_probe_unsent(NAME, p.encode())       # runtime: сокета зонда ещё нет
                elif self.answer_at is not None and now >= self.answer_at:
                    v.reply_in(Reply(LINE, 1, 7, "lead", p.s, 0, 1), now + 0.2)
            if int(now - self.t0) % 5 == 0:
                self.acts += self.c.tick() + s.reserve_code_link_watch()
                if s.reserve_code_window()["open"] and not self.opened:
                    self.opened.append(now - self.t0)
            self.b.advance(1)


def test_a_fresh_probe_without_its_socket_does_not_open_the_window_after_the_return(box, monkeypatch):
    """Ход стенда: зонд свежий, у резерва пользователи основы (кандидата нет); сокет зонда —
    через 35 с, первая проба после него без ответа, дальше ответы; возврат — relay.
    Окна кода нет ни на возврате, ни после; Т-К21 никому."""
    box.s.reserve_set_in("serving")
    w = _World(box, monkeypatch, sock_in=35, answer_in=50)
    w.run(60)
    box.s.reserve_set_in("relay")                         # возврат: зона свободна, кандидат встаёт
    w.run(120)
    assert not w.opened, f"окно кода при живой основе через {w.opened} с после старта зонда: {[n.text for n in notes(w.acts)]}"
    assert not notes(w.acts), [n.text for n in notes(w.acts)]
    assert w.v.view == "see"


def test_the_same_with_the_zone_free_from_the_start(box, monkeypatch):
    """Без переезда (зонд свежий при свободной зоне — пара, новый конфиг): то же."""
    w = _World(box, monkeypatch, sock_in=30, answer_in=45)
    w.run(180)
    assert not w.opened and w.v.view == "see", f"окно: {w.opened}, взгляд {w.v.view}"


def test_a_dead_primary_still_opens_the_window_no_s_after_the_socket_came_up(box, monkeypatch):
    """Честный путь: сокет встал через 35 с, основа мертва — окно открыто и Т-К21 обоим, не
    позже no_s от первой ушедшей пробы и такта кандидата."""
    w = _World(box, monkeypatch, sock_in=35, answer_in=None)
    w.run(200)
    no_s = w.v.timing.no_s
    assert w.opened, "мёртвая основа: окно кода не открылось — пользователи без VPN"
    assert w.opened[0] <= 35 + no_s + 5 + 1, f"окно через {w.opened[0]} с — позже no_s от первой ушедшей пробы"
    assert {n.tg_id for n in notes(w.acts)} >= {PEER_ADMIN}, [n.text for n in notes(w.acts)]


def test_own_permanent_blindness_is_not_her_death(box, monkeypatch):
    """Сокет зонда так и не встал (своя поломка `awg-quo`): «не вижу» о второй стороне и
    окна кода нет — о ней проба ничего не узнала."""
    w = _World(box, monkeypatch, sock_in=10_000, answer_in=None)
    w.run(600)
    assert not w.opened and w.v.view == "new", f"окно {w.opened}, взгляд {w.v.view}"
