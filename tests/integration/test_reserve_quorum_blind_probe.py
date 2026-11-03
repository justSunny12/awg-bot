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

Свой зонд слеп дольше предела (`own_blind_limit`: T_up, не меньше двух повторов
сокета) — сокет не встаёт, пробы не уходят: своего взгляда у кандидата нет, и при
мёртвой основе (связи по линку пары нет дольше T_up) окно кода открывает запасной
сигнал по линку пары с причиной «зонд резерва к серверу не работает» — иначе при
v ≤ 2 у админа не оставалось ни одного пути переезда (`active` из последнего
`st.quo` мёртвой основы так и остаётся true). Живая основа (линк пары на связи) или
шлюз, который её видит, окна не открывают.
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

    def __init__(self, box, monkeypatch, *, sock_in: float, answer_in: float | None, sock_off_in: float | None = None,
                 members: list[str] | None = None):
        monkeypatch.setattr(votermod, "time", types.SimpleNamespace(monotonic=box.clock.monotonic, time=box.clock.time))
        box.peer_admin_opened()
        self.b, self.c = box, Cand(box, members=SOLO if members is None else members)
        self.t0 = box.clock.t
        self.v = Voter(LINE, "a")
        self.v.start(self.t0)
        box.s._qvoters[NAME] = self.v
        self.c.v = self.v
        self.sock_at = self.t0 + sock_in
        self.sock_off_at = None if sock_off_in is None else self.t0 + sock_off_in
        self.answer_at = None if answer_in is None else self.t0 + answer_in
        self.answer_until: float | None = None
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
                if now < self.sock_at or (self.sock_off_at is not None and now >= self.sock_off_at):
                    s.quorum_probe_unsent(NAME, p.encode())       # runtime: сокета зонда нет
                elif self.answer_at is not None and now >= self.answer_at and \
                        (self.answer_until is None or now < self.answer_until):
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


LIMIT = 100                                           # own_blind_limit при умолчаниях: T_up (100) > 2 × 30


def _dead_primary(box) -> None:
    from awgbot.util import timeutil
    box.primary(False, since=timeutil.now_iso())          # линк пары: «нет связи» с этого момента


def test_own_permanent_blindness_with_the_primary_on_the_link_is_not_her_death(box, monkeypatch):
    """Сокет зонда так и не встал (своя поломка `awg-quo`), а линк пары с основой жив: «не вижу» о
    ней и окна кода нет — о ней проба ничего не узнала; карточка говорит о слепом зонде."""
    w = _World(box, monkeypatch, sock_in=10_000, answer_in=None)
    w.run(600)
    assert not w.opened and w.v.view == "new", f"окно {w.opened}, взгляд {w.v.view}"
    assert not notes(w.acts), [n.text for n in notes(w.acts)]
    peer = box.s.reserve_quorum_card()["peer"]
    assert peer.get("blind_since"), f"карточка не знает о слепом зонде: {peer}"
    from awgbot.bot.texts.reserve.section import quorum_peer_lines
    line = "\n".join(quorum_peer_lines(box.s.reserve_quorum_card()))
    assert "Зонд этого сервера" in line and "не работает" in line, line


def test_a_short_blindness_is_not_shown_and_does_not_count(box, monkeypatch):
    """Свежий зонд слеп 35 с (сокет со второй попытки) — меньше предела: в карточке слепоты нет."""
    w = _World(box, monkeypatch, sock_in=35, answer_in=None)
    w.run(30)
    assert not box.s.reserve_quorum_own_blind() and not box.s.reserve_quorum_card()["peer"].get("blind_since")


def test_a_dead_primary_and_a_blind_probe_open_the_window_by_the_link(box, monkeypatch):
    """Основа мертва (линка пары нет), свой зонд слеп навсегда, v = 1: окно кода открывает запасной
    сигнал по линку пары — не раньше предела слепоты и T_up без связи, с причиной; Т-К21 обоим."""
    _dead_primary(box)
    w = _World(box, monkeypatch, sock_in=10_000, answer_in=None)
    w.run(300)
    assert w.opened, "мёртвая основа и слепой зонд: окна кода нет — пользователи без VPN"
    assert LIMIT <= w.opened[0] <= LIMIT + 5 + 1, f"окно через {w.opened[0]} с"
    assert box.s.rsv_get("rsv_view")["code"]["by"] == "link"
    texts = [n.text for n in notes(w.acts)]
    assert {n.tg_id for n in notes(w.acts)} >= {PEER_ADMIN}, texts
    assert any("зонд резерва к серверу не работает" in t for t in texts), texts


def test_two_voters_one_gateway_not_seeing_open_the_window_by_the_link(box, monkeypatch):
    """v = 2 (один шлюз): шлюз «не вижу», свой зонд слеп — большинства «нет» нет (свой голос не
    «нет»), кандидат не решает; основа мертва по линку — окно по линку пары открывается."""
    _dead_primary(box)
    w = _World(box, monkeypatch, sock_in=10_000, answer_in=None, members=["b", "a", "b1"])
    w.acts += w.c.view(1, "uns")
    w.run(300)
    assert w.opened and box.s.rsv_get("rsv_view")["code"]["by"] == "link", f"окно: {w.opened}"


def test_a_gateway_seeing_the_primary_keeps_the_link_window_shut(box, monkeypatch):
    """Свой зонд слеп и линка пары нет, а шлюз основы её видит — она жива (разрез между серверами):
    окна по линку нет; шлюз перестал видеть — окно открывается."""
    _dead_primary(box)
    w = _World(box, monkeypatch, sock_in=10_000, answer_in=None, members=["b", "a", "b1"])
    w.acts += w.c.view(1, "see")
    w.run(300)
    assert not w.opened, f"шлюз видит основу, а окно по линку открыто через {w.opened} с"
    w.acts += w.c.view(1, "uns", since="2026-10-30T10:09:00+03:00")
    w.run(10)
    assert w.opened, "шлюз перестал видеть — окна нет"


def test_a_stale_see_before_the_blindness_does_not_hold_the_window(box, monkeypatch):
    """Зонд видел основу, потом его сокет пропал навсегда (взгляд так и остался «вижу» — проба
    не ушла, пропуска нет), основа умерла: прежний «вижу» после предела не держится — окно по
    линку пары открывается."""
    w = _World(box, monkeypatch, sock_in=0, answer_in=0, sock_off_in=60)
    w.run(60)
    assert w.v.view == "see", "подготовка: зонд основу не видит"
    _dead_primary(box)
    w.run(300)
    assert w.v.view == "see", "подготовка: взгляд слепого зонда сменился"
    assert w.opened, "прежний «вижу» слепого зонда держит окно закрытым при мёртвой основе"


def test_the_probe_coming_back_hands_the_window_over_without_a_new_episode(box, monkeypatch):
    """Окно открыто по линку (основа мертва, зонд слеп); сокет встал, ответов нет: окно держится,
    пока у зонда нет взгляда, и после его «не вижу» переходит кандидату тем же эпизодом — без
    закрытия и без второго Т-К21."""
    _dead_primary(box)
    w = _World(box, monkeypatch, sock_in=200, answer_in=None)
    w.sock_at = w.t0 + 10_000
    w.run(150)
    assert w.opened, "подготовка: окно по линку не открылось"
    n0 = len(notes(w.acts))
    w.sock_at = w.b.clock.t                               # сокет встал
    for _ in range(int(w.v.timing.no_s + 30)):
        w.run(1)
        assert box.s.reserve_code_window()["open"], "окно закрылось, пока у ожившего зонда нет взгляда"
    assert w.v.view == "uns", "подготовка: зонд не сказал «не вижу»"
    assert box.s.rsv_get("rsv_view")["code"]["by"] == "quorum"
    assert len(notes(w.acts)) == n0, [n.text for n in notes(w.acts)[n0:]]
