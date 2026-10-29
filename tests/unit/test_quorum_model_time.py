"""Модельное время: лидер, кандидат и голосующие с одними цифрами и одним
сценарием (util/quorum/leader.py, candidate.py, voter.py; концепт «кворум»
§3.5.3 — запас 35 с, §3.2 — асимметричная потеря, §3.4.3 — обязательство «нет»).

Цена ошибки: главное свойство кворума — «лидер останавливается раньше, чем
кандидат начинает». Нарушено — на секунды или минуты у линии два писателя:
обе стороны пишут в базу и отвечают пользователям, журнал эпох расходится,
а при возврате теряются решения одной из сторон.
"""
from __future__ import annotations

import pytest

from awgbot.util.quorum.candidate import Candidate
from awgbot.util.quorum.leader import Leader
from awgbot.util.quorum.probe import Probe
from awgbot.util.quorum.timing import Timing
from awgbot.util.quorum.voter import Voter

pytestmark = pytest.mark.unit

T = Timing()
E = 12
G = 4
TF = 10_000.0                 # момент, когда последний нужный голос ушёл в uns


def _leader(voters: tuple[str, ...]) -> Leader:
    ld = Leader(l="a", v=len(voters), epoch=E, candidate="b", timing=T)
    ld.set_members(voters, len(voters), G, 0.0)
    return ld


def _candidate(voters: tuple[str, ...]) -> Candidate:
    c = Candidate(l="a", v=len(voters), me="b", timing=T)
    c.set_members(voters, len(voters))
    return c


def _p(ld: Leader, node: str, w: str, now: float, e: int = E) -> str:
    return ld.probe_in(Probe("a", G, node, e, w, 1), now)


# ── запас 35 с ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("phase", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("probe_gap", [9, 15, 21])
@pytest.mark.parametrize("after", ["uns", None])
def test_the_leader_stops_before_the_candidate_starts(phase, probe_gap, after):
    """v = 3: «Beta» (кандидат) уже не видит, Шлюз 2 видит до конца; Шлюз 1 —
    последний, без кого «нет» не большинство, — ушёл в uns в tf, и его последняя
    проба see ушла в tf; дальше его пробы несут uns или не доходят вовсе (тогда
    see истекает только по окну W). До tf лидер держится (две see), лишается
    кворума не позже tf + W + T_down + такт = tf + 65; кандидат забирает не
    раньше tf + T_up = tf + 100 — при любом сдвиге тактов и интервале 9…21 с."""
    voters = ("b", "a1", "a2")
    ld, c = _leader(voters), _candidate(voters)
    start = TF - 300
    for n in voters:
        _p(ld, n, "see", start)
    assert ld.tick(start) == "unfence"
    c.set_own("uns", TF - 200)
    c.view_in("a1", "see", start)
    c.view_in("a2", "see", start)
    fence_at = take_at = None
    now = start + 1
    while now <= TF + 300:
        el = now - TF
        if el < 0 and int(now - start) % probe_gap == 0:
            _p(ld, "a1", "see", now)                # Шлюз 1 видит до tf
        if el == 0:
            _p(ld, "a1", "see", now)                # последняя see — ровно в tf
            c.view_in("a1", "uns", now)             # и в тот же миг «нет» кандидату
        if after and el > 0 and int(el) % probe_gap == 0:
            _p(ld, "a1", after, now)
        if int(now - start) % probe_gap == 3:
            _p(ld, "a2", "see", now)                # Шлюз 2 видит всё время
        if int(now - start) % 15 == 0:
            _p(ld, "b", "uns" if now >= TF - 200 else "see", now)
        if int(now) % T.tick_s == phase:
            if ld.tick(now) == "fence" and fence_at is None:
                fence_at = now
        if int(now) % T.tick_s == (phase + 2) % T.tick_s:
            if "take" in c.tick(now) and take_at is None:
                take_at = now
        now += 1
    assert fence_at is not None and take_at is not None, (fence_at, take_at)
    assert fence_at > TF, f"лидер лишён кворума при двух see: tf − {TF - fence_at}"
    if after is None:
        assert fence_at > TF + T.yes_window_s + T.down_s - 1, "see Шлюза 1 выпала из счёта раньше окна W"
    assert fence_at <= TF + T.yes_window_s + T.down_s + T.tick_s, f"лидер лишён кворума в tf + {fence_at - TF}"
    assert take_at >= TF + T.up_s, f"кандидат забрал в tf + {take_at - TF}"
    assert take_at - fence_at >= T.up_s - T.yes_window_s - T.down_s - T.tick_s, "запас меньше 35 с"


@pytest.mark.parametrize("phase", [0, 2, 4])
def test_case_three_leader_stops_before_the_candidate_starts(phase):
    """Случай 3, v = 3: шлюзы ушли в uns в tf (последние see — в tf), кандидат видит
    лидера и шлёт see. Лидер держится T_down_rf и лишается кворума не позже
    tf + W + 820 + такт = tf + 865; кандидат забирает не раньше tf + 900."""
    voters = ("b", "a1", "a2")
    ld, c = _leader(voters), _candidate(voters)
    start = TF - 100
    for n in voters:
        _p(ld, n, "see", start)
    ld.tick(start)
    c.set_own("see", start)
    for n in ("a1", "a2"):
        _p(ld, n, "see", TF)
        c.view_in(n, "uns", TF)
    fence_at = take_at = None
    now = TF
    while now <= TF + 1200:
        if int(now - TF) % 15 == 0:
            _p(ld, "b", "see", now)
            if now > TF:
                for n in ("a1", "a2"):
                    _p(ld, n, "uns", now)
        if int(now) % T.tick_s == phase and ld.tick(now) == "fence" and fence_at is None:
            fence_at = now
        if int(now) % T.tick_s == (phase + 3) % T.tick_s and "take" in c.tick(now) and take_at is None:
            take_at = now
        now += 1
    assert fence_at is not None and fence_at > TF + T.yes_window_s + T.down_s, "в случае 3 сработал короткий T_down"
    assert fence_at <= TF + T.yes_window_s + T.down_rf_s + T.tick_s, \
        f"лидер в случае 3 лишён кворума в tf + {fence_at and fence_at - TF}"
    assert take_at is not None and take_at >= TF + T.rf_block_s, f"кандидат забрал в tf + {take_at and take_at - TF}"


# ── асимметричная потеря ──────────────────────────────────────────────────────

def test_a_lost_reply_never_lets_one_voter_count_twice():
    """Проба доходит, ответ — нет: голосующий через reply_s — sus, через no_s —
    uns; его пробы несут sus/uns, и в каждую секунду он не одновременно «да» у
    лидера и «не вижу» (то, что уходит кандидату)."""
    voters = ("b", "a1")
    ld = _leader(voters)
    vt = Voter(l="a", me="a1", timing=T)
    vt.start(TF - 100)
    vt.epoch_in(E)
    lost_from = TF
    views, overlap = [], []
    now = TF - 100
    while now <= TF + 200:
        acts = vt.tick(now)
        views += [(now, a) for a in acts if a in ("see", "sus", "uns")]
        if "probe" in acts:
            w = vt.w_for_probe()                    # взгляд до этой пробы
            s = vt.probe_out(now, T.probe_s)
            p = Probe("a", G, "a1", vt.epoch, w, s)
            ld.probe_in(p, now)                     # проба доходит всегда
            if now < lost_from:
                views += [(now, a) for a in vt.reply_in(ld.reply_for(p, now), now + 0.2)]
        if vt.view == "uns" and "a1" in ld.yes_nodes(now):
            overlap.append(now)
        now += 1
    kinds = [a for _, a in views]
    assert kinds == ["see", "sus", "uns"], f"взгляды голосующего: {views}"
    sus_at, uns_at = views[1][0], views[2][0]
    assert sus_at - TF <= T.probe_s + T.reply_s + 1, "sus не по первому пропущенному ответу"
    assert uns_at - sus_at <= T.no_s, "uns позже no_s после пропуска"
    assert not overlap, f"голос засчитан дважды в {overlap[:3]}"
    assert ld.yes_nodes(uns_at) == (), "лидер считает «да» голосующего, ушедшего в uns"


# ── обязательство «нет» ──────────────────────────────────────────────────────

def test_network_restored_before_qtake_does_not_bring_the_old_leader_back():
    """v = 3: шлюзы отдали «нет» под эпохой 12 и держат обязательство; сеть
    вернулась до `qtake` — их пробы к прежнему лидеру идут с held, кандидат
    (уже забирающий) шлёт эпоху 13. Лидер без права записи не возвращается,
    пока обязательство держится."""
    voters = ("b", "a1", "a2")
    ld = _leader(voters)
    for n in voters:
        _p(ld, n, "uns", TF - 100)
    ld.fence_now(TF - 100)
    gws = {}
    for n in ("a1", "a2"):
        vt = Voter(l="a", me=n, timing=T)
        vt.start(TF - 100)
        vt.epoch_in(E)
        for k in range(0, 60):
            vt.tick(TF - 100 + k)
        assert vt.view == "uns"
        vt.said_no(TF - 40)
        gws[n] = vt
    back = []
    now = TF
    while now < TF - 40 + T.pledge_s:
        for n, vt in gws.items():
            if "probe" in vt.tick(now):
                w = vt.w_for_probe()
                s = vt.probe_out(now, T.probe_s)
                p = Probe("a", G, n, vt.epoch, w, s)
                ld.probe_in(p, now)
                vt.reply_in(ld.reply_for(p, now), now + 0.2)
        if int(now) % 15 == 0:
            assert _p(ld, "b", "see", now, e=E + 1) == "adopt"
        if ld.tick(now) == "unfence":
            back.append(now)
        now += 1
    assert all(vt.view == "see" for vt in gws.values()), "сеть восстановилась, а взгляд не вернулся"
    assert not back, f"прежний лидер вернулся к записи в tf + {back[0] - TF}"
    # qtake{12}: шлюзы принимают 13, их пробы прежнему лидеру — уже с чужой эпохой
    for vt in gws.values():
        vt.take(E)
        assert vt.epoch == E + 1 and vt.w_for_probe() == "see"
        assert ld.probe_in(Probe("a", G, vt.me, vt.epoch, vt.w_for_probe(), 1), now) == "adopt"
    assert ld.yes_count(now) == 0 and ld.tick(now) == ""
