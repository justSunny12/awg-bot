"""
util/backoff.jitter — общий разброс сроков сетевых задач (повтор захвата
Telegram, доделывание приёма после смены записи, проба зависшего вебхука у
резерва; проба и повтор опроса кворума — тот же `jitter` через
util/quorum/timing): base ± 40 % равномерно, один вызов
`random.uniform(1 − frac, 1 + frac)`, свой источник `rnd` — края разброса.

Цена ошибки: строго периодический запрос в сети — маячок для наблюдателя;
разброс шире заявленного — проба кворума опаздывает к сроку «не вижу»
(no_s), уже — расписание снова видно; две копии формулы расходятся, и
кворум и резерв считают «± 40 %» по-разному.
"""
from __future__ import annotations

import pytest

from awgbot.util import backoff
from awgbot.util.quorum import timing

pytestmark = pytest.mark.unit


def test_the_jitter_is_one_uniform_call_within_forty_percent(monkeypatch):
    seen = []
    monkeypatch.setattr(backoff.random, "uniform", lambda a, b: seen.append((a, b)) or 1.25)
    assert backoff.jitter(60) == pytest.approx(75.0)
    assert seen == [(0.6, 1.4)], seen
    seen.clear()
    assert backoff.jitter(10, frac=0.1) == pytest.approx(12.5)
    assert seen == [(0.9, 1.1)], seen


def test_the_spread_is_real_and_bounded():
    vals = [backoff.jitter(30) for _ in range(2000)]
    assert 18.0 <= min(vals) and max(vals) <= 42.0
    assert max(vals) - min(vals) > 12, "джиттер не разбрасывает интервал"


def test_an_own_source_gives_the_edges():
    assert backoff.jitter(15, rnd=lambda: 0.0) == pytest.approx(9.0)
    assert backoff.jitter(15, rnd=lambda: 1.0) == pytest.approx(21.0)
    assert backoff.jitter(15, rnd=lambda: 0.5) == pytest.approx(15.0)


def test_the_quorum_uses_the_same_jitter():
    assert timing.jitter is backoff.jitter, "у кворума своя копия формулы разброса"
