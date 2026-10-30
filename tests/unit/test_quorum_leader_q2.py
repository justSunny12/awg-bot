"""Ответчик дома в отъезде — счёт «да» для возврата «сразу» (Q2): правило
лидера в режиме `q2` (util/quorum/leader.py; концепт «кворум» §3.7.8).

Пока пользователи основы у резерва, голосующие продолжают пробовать её
интерфейс AWG; ответчик дома считает их «да» без сверки эпохи (эпоху линии в
отъезде держит бот основы на резерве), в ответе — роль `follow`, решений нет.

Цена ошибки: «да» в отъезде, отброшенные за «чужую» эпоху, — основа никогда
не видит себя здоровой и пользователи остаются у резерва, пока админ не
вернёт их руками; ответ с ролью `lead` или `fenced` из отъезда — голосующие
принимают дом за лидера при живом клоне; ограждение в режиме Q2 — машина
«решает» там, где решать нечего.
"""
from __future__ import annotations

import pytest

from awgbot.util.quorum.leader import Leader
from awgbot.util.quorum.members import majority
from awgbot.util.quorum.probe import Probe
from awgbot.util.quorum.timing import Timing

pytestmark = pytest.mark.unit

T = Timing()
EPOCH = 12
T0 = 5000.0
VOTERS = ("b", "a1", "a2")


def _q2() -> Leader:
    ld = Leader(l="a", v=3, epoch=EPOCH, candidate="b", timing=T, q2=True)
    ld.set_members(VOTERS, 3, 1, 0.0)
    return ld


def _probe(ld: Leader, node: str, *, e: int, w: str = "see", now: float = T0) -> str:
    return ld.probe_in(Probe("a", 1, node, e, w, 1), now)


def test_in_q2_a_yes_counts_whatever_epoch_the_probe_carries():
    """Голосующие в отъезде знают эпоху клона (больше эпохи основы) — их «да» об основе
    всё равно «да»."""
    ld = _q2()
    _probe(ld, "b", e=EPOCH + 1)
    _probe(ld, "a1", e=EPOCH - 3)
    assert ld.yes_count(T0) == 2, "Q2 отбросил «да» за несовпавшую эпоху"


def test_outside_q2_the_same_probes_do_not_count():
    """Контроль: обычный лидер сверяет эпоху — иначе тест выше ничего не проверяет."""
    ld = Leader(l="a", v=3, epoch=EPOCH, candidate="b", timing=T)
    ld.set_members(VOTERS, 3, 1, 0.0)
    _probe(ld, "b", e=EPOCH + 1)
    _probe(ld, "a1", e=EPOCH - 3)
    assert ld.yes_count(T0) == 0


def test_in_q2_the_reply_role_is_follow():
    ld = _q2()
    p = Probe("a", 1, "a1", EPOCH, "see", 7)
    ld.probe_in(p, T0)
    r = ld.reply_for(p, T0)
    assert r.r == "follow", f"ответ из отъезда несёт роль {r.r!r} — голосующий примет дом за лидера"
    assert r.s == 7 and r.y == 1 and r.v == 3


def test_in_q2_the_role_is_follow_even_while_the_machine_is_fenced():
    """Машина стартует «без права записи»; в отъезде это не должно просочиться в
    ответ: `fenced` голосующие сочли бы за лишённого кворума лидера-дом."""
    ld = _q2()
    assert ld.fenced, "предпосылка: машина на старте без права записи"
    assert ld.role() == "follow"
    ld.fence_now(T0)
    assert ld.role() == "follow"


def test_q2_yes_strictly_more_than_half():
    """Порог Q2 — «да» строго больше половины v (не «≥ v/2», как держится лидер)."""
    ld = _q2()
    _probe(ld, "b", e=EPOCH)
    assert not majority(ld.yes_count(T0), ld.v), "одно «да» из трёх — уже «здоров»"
    _probe(ld, "a1", e=EPOCH)
    assert majority(ld.yes_count(T0), ld.v)


def test_q2_yes_older_than_the_window_is_gone():
    ld = _q2()
    _probe(ld, "b", e=EPOCH, now=T0)
    _probe(ld, "a1", e=EPOCH, now=T0)
    assert ld.yes_count(T0 + T.yes_window_s) == 2
    assert ld.yes_count(T0 + T.yes_window_s + 1) == 0, "«да» старше окна W держит основу «здоровой»"
