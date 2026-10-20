"""Живость линка пары — чистая машина `step` (domain/services/reserve/liveness.py)
на заглушке счётчика `rx`.

Сценарии — по такту в 5 с, как у задания планировщика: keepalive второй
стороны идёт раз в 25–35 с, тишина ≥ 40 с — подозрение и проба `ping`
(трижды через 5 с), нет ответа 15 с — «недоступна», жизнь после этого —
«снова на связи».

Цена ошибки: ложное «недоступна» от одной потерянной keepalive — со звуком
админу, а в следующих этапах это голос за переезд клиентов; проба без
подозрения — прикладное сообщение в простое по ровному такту, тот самый
маячок, которого канал пары избегает; «недоступна» без пробы — секунды
до ложного переезда.
"""
from __future__ import annotations

import random

import pytest

from awgbot.domain.services.reserve import liveness as lvm

pytestmark = pytest.mark.unit

T0 = 10_000.0
SUSPECT, PROBE = 40, 15


def _run(lv, until: float, rx_at, *, start: float = T0, answer=None) -> list[tuple[float, str]]:
    """Такты по 5 с от start до until; rx_at(t) — счётчик линка в момент t;
    answer(t, act) — что сделать после действия (ответ на пробу). Возвращает
    (время, действие) непустых действий."""
    out = []
    t = start
    while t <= until:
        act = lvm.step(lv, t, rx_at(t), suspect_s=SUSPECT, probe_s=PROBE)
        if act:
            out.append((t, act))
            if answer is not None:
                answer(t, act)
        t += 5
    return out


def _alive_once(lv):
    """Первый такт: счётчик прочитан — сторона жива на T0."""
    assert lvm.step(lv, T0, 100, suspect_s=SUSPECT, probe_s=PROBE) == ""


def test_39_seconds_of_silence_is_not_a_suspicion():
    lv = lvm.Liveness()
    _alive_once(lv)
    for t in (T0 + 5, T0 + 20, T0 + 35, T0 + 39):
        assert lvm.step(lv, t, 100, suspect_s=SUSPECT, probe_s=PROBE) == "", f"действие на {t - T0:.0f} с тишины"
    assert lv.state == "ok", "39 с тишины — уже подозрение"


def test_40_seconds_of_silence_is_a_suspicion_and_sends_the_first_probe():
    lv = lvm.Liveness()
    _alive_once(lv)
    assert lvm.step(lv, T0 + 40, 100, suspect_s=SUSPECT, probe_s=PROBE) == "ping"
    assert lv.state == "suspect" and lv.pings == 1 and lv.nonce, "подозрение без пробы с нонсом"


def test_no_answer_for_15_seconds_is_down_after_three_probes():
    """Подозрение на 40 с, пробы на 40/45/50, «недоступна» ровно через 15 с
    подозрения — на 55 с, один раз; дальше машина ждёт жизни молча."""
    lv = lvm.Liveness()
    _alive_once(lv)
    acts = _run(lv, T0 + 120, lambda t: 100, start=T0 + 5)
    assert acts == [(T0 + 40, "ping"), (T0 + 45, "ping"), (T0 + 50, "ping"), (T0 + 55, "down")], acts
    assert lv.state == "down" and lv.t0 == T0 + 55


def test_a_growing_counter_answers_the_probe_and_lifts_the_suspicion():
    lv = lvm.Liveness()
    _alive_once(lv)
    assert lvm.step(lv, T0 + 40, 100, suspect_s=SUSPECT, probe_s=PROBE) == "ping"
    assert lvm.step(lv, T0 + 45, 160, suspect_s=SUSPECT, probe_s=PROBE) == "", "ответ на пробу дал действие"
    assert lv.state == "ok" and lv.pings == 0, "рост rx не снял подозрение"
    acts = _run(lv, T0 + 80, lambda t: 160, start=T0 + 50)
    assert acts == [], f"после ответа тишина меньше 40 с — а действия были: {acts}"


def test_life_after_down_is_up_once():
    lv = lvm.Liveness()
    _alive_once(lv)
    _run(lv, T0 + 60, lambda t: 100, start=T0 + 5)
    assert lv.state == "down"
    assert lvm.step(lv, T0 + 65, 101, suspect_s=SUSPECT, probe_s=PROBE) == "up"
    assert lvm.step(lv, T0 + 70, 102, suspect_s=SUSPECT, probe_s=PROBE) == "", "«снова на связи» дважды"


def test_a_reset_counter_is_neither_life_nor_a_reason_to_wait_forever():
    """Линк переподнят — счётчик с нуля: это не признак жизни второй стороны
    (её пакетов не было), но и не застывший счётчик: следующий рост от нового
    нуля — жизнь."""
    lv = lvm.Liveness()
    _alive_once(lv)
    assert lvm.step(lv, T0 + 30, 5, suspect_s=SUSPECT, probe_s=PROBE) == ""
    assert lvm.step(lv, T0 + 40, 5, suspect_s=SUSPECT, probe_s=PROBE) == "ping", \
        "обнулённый счётчик засчитан жизнью — подозрение не пришло"
    assert lvm.step(lv, T0 + 45, 9, suspect_s=SUSPECT, probe_s=PROBE) == ""
    assert lv.state == "ok", "рост от нового нуля не снял подозрение"


def test_an_unreadable_counter_counts_as_silence():
    """`awg show` не ответил (None) — не жизнь: интерфейса нет — второй стороны не слышно."""
    lv = lvm.Liveness()
    _alive_once(lv)
    acts = _run(lv, T0 + 60, lambda t: None, start=T0 + 5)
    assert [a for _, a in acts][-1] == "down", acts


def test_probes_stop_at_three_however_long_the_probe_window():
    lv = lvm.Liveness()
    _alive_once(lv)
    acts = _run(lv, T0 + 300, lambda t: 100, start=T0 + 5)
    pings = [t for t, a in acts if a == "ping"]
    assert len(pings) == 3, f"проб {len(pings)} — потолок 3"
    assert all(b - a >= lvm.PROBE_EVERY for a, b in zip(pings, pings[1:])), pings


def _keepalives(seed: int, hours: float, lost: set[int] = frozenset()):
    """Моменты keepalive второй стороны раз в 25–35 с за hours часов; lost —
    номера потерянных (до нас не дошли)."""
    rnd = random.Random(seed)
    t, n, out = T0, 0, []
    while t < T0 + hours * 3600:
        t += rnd.uniform(25, 35)
        n += 1
        if n not in lost:
            out.append(t)
    return out


def _counter(arrivals: list[float], extra: list[float]):
    def rx_at(t: float) -> int:
        return 100 + 148 * sum(1 for a in arrivals if a <= t) + 92 * sum(1 for a in extra if a <= t)
    return rx_at


@pytest.mark.parametrize("seed", range(5))
def test_one_lost_keepalive_never_gives_down(seed):
    """Потеряна одна keepalive (обычная потеря UDP) — разрыв до ~70 с: может
    прийти подозрение, но проба будит рукопожатие и ответ приходит к
    следующему такту — «недоступна» не бывает ни разу. Терять — каждую
    седьмую, по одной, весь час."""
    arrivals = _keepalives(seed, 1, lost=set(range(7, 200, 7)))
    replies: list[float] = []

    def answer(t, act):
        if act == "ping":
            replies.append(t + 0.3)                     # ответ за RTT — пакеты по линку
    lv = lvm.Liveness()
    acts = _run(lv, T0 + 3600, _counter(arrivals, replies), answer=answer)
    assert "down" not in [a for _, a in acts], f"одна потерянная keepalive дала «недоступна»: {acts}"


@pytest.mark.parametrize("seed", range(5))
def test_an_idle_hour_with_a_healthy_peer_sends_no_probe(seed):
    """Сторож маячка: час простоя с живой второй стороной — ни одной пробы
    (`ping` — единственное прикладное сообщение, которое шлёт такт живости)."""
    lv = lvm.Liveness()
    acts = _run(lv, T0 + 3600, _counter(_keepalives(seed, 1), []))
    assert acts == [], f"без подозрения такт что-то отправил: {acts[:5]}"
