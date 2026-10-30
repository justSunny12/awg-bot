"""Чистые правила кворума — граничные случаи, которых нет в таблицах истинности
(util/quorum/leader.py, candidate.py, voter.py, members.py): шесть голосующих,
смена состава на лету у лидера и у кандидата, узел вне линии, сброс счёта,
свой взгляд, вернувшийся при большинстве «нет», «вижу» в обязательстве,
номер пробы на переполнении, пауза до первого ответа, повтор опроса.

Машины без ввода-вывода; время — аргумент `now`.

Цена ошибки: при v = 6 лидер держится с двумя «да» из шести — бот пишет
решения, хотя большинство его не видит; кандидат забирает при трёх «нет» из
шести — второй писатель при живой основе; голос узла, снятого из состава,
остаётся в счёте — переезд по слову шлюза, которого в линии уже нет; свой
«вижу» при большинстве «нет» шлюзов не переводит кандидата на длинный таймер
случая 3 — пользователи уезжают с живой основы через 100 с вместо 15 мин;
номер пробы после переполнения не совпадает с ответом — голосующий, работающий
месяцами, однажды перестаёт видеть лидера.
"""
from __future__ import annotations

import itertools

import pytest

from awgbot.util.quorum import members as qm
from awgbot.util.quorum.candidate import Candidate
from awgbot.util.quorum.leader import Leader
from awgbot.util.quorum.members import GenWindow, half_or_more, majority
from awgbot.util.quorum.probe import Probe, Reply
from awgbot.util.quorum.timing import Timing
from awgbot.util.quorum.voter import Voter

pytestmark = pytest.mark.unit

T = Timing()
T0 = 1000.0
EPOCH = 12


# ── лидер ────────────────────────────────────────────────────────────────────

def _voters(v: int) -> tuple[str, ...]:
    return ("b",) + tuple(f"a{i}" for i in range(1, v))


def _lead(v: int, g: int = 4) -> Leader:
    ld = Leader(l="a", v=v, epoch=EPOCH, candidate="b", timing=T)
    ld.set_members(_voters(v), v, g, 0.0)
    for n in ld.voters:
        ld.probe_in(Probe("a", g, n, EPOCH, "see", 1), T0 - 100)
    assert ld.tick(T0 - 100) == "unfence", "подготовка: лидер не снял стартовое ограждение"
    return ld


def _probe(ld: Leader, node: str, w: str, now: float, g: int | None = None) -> str:
    return ld.probe_in(Probe("a", ld.gen.g if g is None else g, node, EPOCH, w, 1), now)


def test_six_voters_keep_the_leader_with_three_yes_and_not_with_two():
    """v = 6 (чётное, наибольший ансамбль слотов в живой эксплуатации): лидер держится
    при «да» ≥ 3 и лишается кворума через T_down при двух «да» без кандидата; при «да»
    кандидата — держится до T_down_rf. Все сочетания see / uns / молчит."""
    v = 6
    for combo in itertools.product(("see", "uns", None), repeat=v):
        ld = _lead(v)
        for node, w in zip(ld.voters, combo):
            if w is not None:
                _probe(ld, node, w, T0)
        yes = sum(1 for w in combo if w == "see")
        r = (ld.tick(T0), ld.tick(T0 + T.down_s))
        if yes >= 3 or combo[0] == "see":
            assert r == ("", ""), f"{combo}: «да» {yes} из 6 — лидер не держится {r}"
        else:
            assert r == ("", "fence"), f"{combo}: «да» {yes} из 6 — лишения через T_down нет {r}"


def test_a_grown_composition_is_counted_against_the_new_v():
    """Шлюзы a3, a4 подключены (поколение 5): прежние два «да» из трёх были
    большинством, из пяти — нет. Голоса прежнего поколения в окне ещё считаются,
    но знаменатель — новый: через T_down лидер лишается кворума."""
    ld = _lead(3)
    _probe(ld, "a1", "see", T0)
    _probe(ld, "a2", "see", T0)
    assert ld.tick(T0) == "" and not ld.fenced
    ld.set_members(_voters(5), 5, 5, T0)
    assert ld.v == 5 and ld.yes_count(T0 + 1) == 2, "голоса прежнего поколения в окне не в счёте"
    assert ld.tick(T0 + 1) == ""
    assert ld.tick(T0 + 1 + T.down_s) == "fence", "два «да» из пяти держат лидера после смены состава"


def test_a_shrunk_composition_brings_the_majority_back_after_u():
    """Шлюзы сняты (v 5 → 3): те же два «да» снова большинство — возврат к записи
    через окно U, не мгновенно (это не старт)."""
    ld = _lead(5)
    _probe(ld, "a1", "see", T0)
    _probe(ld, "a2", "see", T0)
    ld.tick(T0)
    assert ld.tick(T0 + T.down_s) == "fence"
    for n in ("a3", "a4"):
        _probe(ld, n, "see", T0 + T.down_s + 1)           # снятые шлюзы — их пробы не в счёте после смены
    ld.set_members(_voters(3), 3, 5, T0 + T.down_s + 2)
    assert "a3" not in ld.seen and "a4" not in ld.seen, "проба снятого узла осталась в памяти лидера"
    now = T0 + T.down_s + 2
    _probe(ld, "a1", "see", now, g=5)
    _probe(ld, "a2", "see", now, g=5)
    assert ld.tick(now) == "", "после лишения возврат без окна U"
    assert ld.tick(now + T.unfence_s) == "unfence", "большинство в новом составе не вернуло лидера"


def test_a_probe_of_a_node_removed_from_the_line_is_ignored():
    ld = _lead(3)
    ld.set_members(_voters(2), 2, 5, T0)
    assert _probe(ld, "a2", "see", T0, g=5) == "ignore", "проба снятого шлюза принята"
    assert "a2" not in ld.seen


def test_the_previous_generation_is_not_accepted_before_any_generation():
    """Окно прежнего поколения не открывается с нуля: первое поколение — прежнего нет."""
    w = GenWindow()
    w.bump(1, T0, 120)
    assert w.accepts(1, T0) and not w.accepts(0, T0 + 1), "поколение 0 принято в окне первого поколения"


# ── кандидат ─────────────────────────────────────────────────────────────────

def _cand(v: int) -> Candidate:
    c = Candidate(l="a", v=v, me="b", timing=T)
    c.set_members(("b",) + tuple(f"a{i}" for i in range(1, v)), v)
    return c


def _run(c: Candidate, start: float, end: float, step: float = 5.0) -> list[tuple[float, str]]:
    out, now = [], start
    while now <= end:
        out += [(now, a) for a in c.tick(now)]
        now += step
    return out


def _first(ev, kind: str) -> float | None:
    return next((t for t, a in ev if a == kind or a.startswith(kind + ":")), None)


@pytest.mark.parametrize("k", range(0, 6))
def test_six_voters_need_four_noes_to_take(k):
    """v = 6: свой «не вижу» и k шлюзов «не вижу», остальные видят. «Забираю» через
    T_up — только при «нет» ≥ 4 (строго больше половины); 3 на 3 — ничья, без
    переезда; меньше — ничего."""
    c = _cand(6)
    c.set_own("uns", T0)
    for i in range(1, 6):
        c.view_in(f"a{i}", "uns" if i <= k else "see", T0)
    ev = _run(c, T0, T0 + T.tie_s + 10)
    no = 1 + k
    if majority(no, 6):
        assert _first(ev, "take") == T0 + T.up_s, f"«нет» {no} из 6 — «забираю» не через T_up: {ev}"
    else:
        assert _first(ev, "take") is None, f"«нет» {no} из 6 — «забираю» без большинства"
    if no == 3:
        assert _first(ev, "tie") == T0 + T.tie_s, f"3 на 3 при v = 6 — ничьей нет: {ev}"
    else:
        assert _first(ev, "tie") is None, f"«нет» {no} из 6 — ничья, которой нет"


def test_a_view_of_a_node_outside_the_line_is_not_counted():
    """Слово шлюза другой стороны или снятого слота (гостевой канал мог отстать от
    состава) — не голос."""
    c = _cand(2)
    c.set_own("uns", T0)
    c.view_in("a5", "uns", T0)
    c.channel("a5", True)
    assert "a5" not in c.votes and c.no_nodes() == ("b",), c.votes
    assert _first(_run(c, T0, T0 + T.up_s + 10), "take") is None, "«нет» узла вне линии дало переезд"


def test_a_gateway_removed_from_the_line_takes_its_no_away_and_releases():
    """v = 3: свой «не вижу» и «нет» a1 — большинство; a1 снят из состава (v = 2) —
    его «нет» уходит, большинства нет: таймер сначала и `rel`."""
    c = _cand(3)
    c.set_own("uns", T0)
    c.view_in("a1", "uns", T0)
    c.view_in("a2", "see", T0)
    c.tick(T0)
    assert c.no_since == T0
    c.set_members(("b", "a2"), 2)
    assert "a1" not in c.votes, "голос снятого шлюза остался"
    ev = _run(c, T0 + 5, T0 + T.up_s + 10)
    assert ev[0] == (T0 + 5, "rel"), f"снятие шлюза не отпустило большинство: {ev[:3]}"
    assert _first(ev, "take") is None


def test_reset_lets_a_held_majority_take_again_after_t_up():
    """После решения (взято или отказано) счёт заново: держащееся большинство даёт
    новое «забираю» не раньше, чем через полный T_up от сброса."""
    c = _cand(2)
    c.set_own("uns", T0)
    c.view_in("a1", "uns", T0)
    assert _first(_run(c, T0, T0 + T.up_s), "take") == T0 + T.up_s
    c.reset()
    assert c.no_since is None and not c.decided and not c.asked
    t1 = T0 + T.up_s + 5
    ev = _run(c, t1, t1 + T.up_s + 10)
    assert _first(ev, "take") == t1 + T.up_s, f"после сброса «забираю» не через полный T_up: {ev}"


def test_own_see_back_inside_the_majority_moves_to_the_long_timer():
    """v = 3: свой «не вижу» и оба шлюза «не вижу» — короткий таймер; свой взгляд
    вернулся к «вижу», шлюзы по-прежнему нет — это случай 3: забираем только через
    T_up_rf от возврата, а не по прежнему T_up."""
    c = _cand(3)
    c.set_own("uns", T0)
    c.view_in("a1", "uns", T0)
    c.view_in("a2", "uns", T0)
    c.tick(T0)
    c.set_own("see", T0 + 50)
    ev = _run(c, T0 + 50, T0 + 50 + T.rf_block_s + 10)
    assert c.no_since is None, "короткий таймер не снят своим «вижу»"
    assert _first(ev, "take") == T0 + 50 + T.rf_block_s, f"свой «вижу» не перевёл на T_up_rf: {_first(ev, 'take')}"


def test_held_of_a_gateway_counts_as_seeing():
    """`held` — «вижу, но держу обязательство»: для кандидата это «вижу». v = 2:
    свой «не вижу» и `held` шлюза — ничья, не большинство."""
    c = _cand(2)
    c.set_own("uns", T0)
    c.view_in("a1", "held", T0)
    assert c.see_nodes() == ("a1",) and c.no_nodes() == ("b",)
    ev = _run(c, T0, T0 + T.tie_s + 10)
    assert _first(ev, "take") is None and _first(ev, "tie") == T0 + T.tie_s, ev


def test_pledged_names_only_live_channels_and_unpledge_clears_them():
    c = _cand(3)
    c.view_in("a1", "uns", T0)
    c.view_in("a2", "uns", T0)
    c.channel("a2", False)
    assert c.pledged() == ["a1"], "обязательство мёртвого канала в списке для qrel/qtake"
    c.unpledge(["a1", "a9"])
    assert c.pledged() == [] and not c.votes["a1"].said_no
    c.channel("a2", True)
    assert c.pledged() == ["a2"], "обязательство вернувшегося канала потеряно"


def test_the_ask_is_repeated_not_on_a_fixed_grid():
    """Повтор опроса — 30 с ± 40 % (как интервал пробы): строго периодический
    `qask` в канале выглядит маячком. Пять повторов подряд не должны идти ровно через
    одно и то же число секунд."""
    c = _cand(4)
    c.set_own("uns", T0)
    c.view_in("a1", "uns", T0)
    ev = _run(c, T0, T0 + 200, 1.0)
    asks = [t for t, a in ev if a.startswith("ask:")]
    gaps = {round(b - a) for a, b in zip(asks, asks[1:])}
    assert len(asks) >= 4, asks
    assert all(18 <= g <= 42 for g in gaps), f"повтор опроса вне 30 с ± 40 %: {sorted(gaps)}"
    assert len(gaps) > 1, f"повтор опроса строго через {gaps} с — без джиттера"


# ── голосующий ───────────────────────────────────────────────────────────────

def _voter() -> Voter:
    v = Voter(l="a", me="a1", timing=T)
    v.start(T0)
    v.epoch_in(EPOCH)
    return v


def test_the_probe_number_wraps_and_still_matches_its_reply():
    v = _voter()
    v.seq = 2 ** 31 - 1
    s = v.probe_out(T0, T.probe_s)
    assert s == 0, s
    p = Probe.parse(Probe("a", 4, "a1", EPOCH, v.w_for_probe(), s).encode())
    assert p is not None and p.s == 0, "проба с номером 0 после переполнения не разбирается"
    raw = Reply("a", 4, EPOCH, "lead", 0, 2, 3).encode()
    v.reply_in(Reply.parse(raw), T0 + 0.1)
    assert v.last_reply == T0 + 0.1, "ответ на пробу после переполнения номера не сопоставлен"


def test_the_first_reply_lifts_a_pause_before_its_term():
    """Пауза лидера (`qpause`) — до срока или до первого ответа: лидер вернулся
    раньше — следующий провал снова считается без отсрочки."""
    v = _voter()
    v.pause(T0, 240)
    assert v.paused(T0 + 1)
    s = v.probe_out(T0 + 2, T.probe_s)
    v.reply_in(Reply("a", 4, EPOCH, "lead", s, 2, 3), T0 + 3)
    assert not v.paused(T0 + 4), "ответ лидера не снял паузу"
    acts = [(k, a) for k in range(4, 200) for a in v.tick(T0 + k) if a == "uns"]
    assert acts and acts[0][0] == 3 + T.no_s, f"после снятой паузы «не вижу» не через T_no от ответа: {acts}"


def test_a_reply_of_a_lower_epoch_does_not_lower_the_known_one():
    v = _voter()
    s = v.probe_out(T0, T.probe_s)
    assert v.reply_in(Reply("a", 4, EPOCH - 3, "lead", s, 2, 3), T0 + 1) == []
    assert v.epoch == EPOCH and not v.epoch_in(EPOCH - 1), "эпоха голосующего пошла назад"


def test_a_late_reply_after_the_loss_is_not_a_see():
    """Ответ, пришедший позже `reply_s` (проба уже засчитана пропуском), — не «вижу»:
    иначе асимметричная задержка давала бы «вижу» и «пропуск» одной пробе."""
    v = _voter()
    s = v.probe_out(T0, T.probe_s)
    v.tick(T0 + T.reply_s)
    assert v.reply_in(Reply("a", 4, EPOCH, "lead", s, 2, 3), T0 + T.reply_s + 0.5) == []
    assert v.last_reply == 0.0


# ── состав ───────────────────────────────────────────────────────────────────

def test_a_gateway_node_of_an_unknown_side_is_refused():
    with pytest.raises(ValueError):
        qm.gateway_node("c", 1)
    assert qm.gateway_node("b", 7) == "b7"


@pytest.mark.parametrize("v,t", [(2, 2), (3, 2), (4, 3), (5, 3), (6, 4)])
def test_threshold_and_the_two_majorities_from_two_to_six(v, t):
    """Порог t — строгое большинство; лидер держится при «да» ≥ v/2 (а не ≥ t):
    при чётном v между ними ничья, при нечётном они совпадают."""
    assert qm.threshold(v) == t
    assert [k for k in range(v + 1) if majority(k, v)][0] == t
    first_hold = [k for k in range(v + 1) if half_or_more(k, v)][0]
    assert first_hold == (v + 1) // 2, (v, first_hold)
    assert (first_hold == t) == (v % 2 == 1), f"v={v}: держится с {first_hold}, порог {t}"
