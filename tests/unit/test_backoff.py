"""
util/backoff.jitter — общий разброс сроков сетевых задач (повтор захвата
Telegram, доделывание приёма после смены записи, проба зависшего вебхука у
резерва; проба и повтор опроса кворума — тот же `jitter` через
util/quorum/timing): base ± 40 % равномерно, один вызов
`random.uniform(1 − frac, 1 + frac)`, свой источник `rnd` — края разброса.

`backoff_delay(table, attempt)` — пауза переподключения каналов (линк шлюза
— своя таблица, пара и гость — общая RECONNECT_S): шаг по номеру попытки,
за краем таблицы — потолок, тот же разброс. Сторож: формулы
`* random.uniform(…)` вне util/backoff больше нет (кроме перечисленных ниже
несимметричных и машины резерва) — каждая копия разъезжалась бы в долях и
подменах тестов.

Цена ошибки: строго периодический запрос в сети — маячок для наблюдателя;
разброс шире заявленного — проба кворума опаздывает к сроку «не вижу»
(no_s), уже — расписание снова видно; две копии формулы расходятся, и
кворум и резерв считают «± 40 %» по-разному.
"""
from __future__ import annotations

import re
from pathlib import Path

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


# ── пауза переподключения по таблице ────────────────────────────────────────

def test_the_reconnect_delay_walks_the_table_and_stops_at_the_ceiling(monkeypatch):
    seen = []
    monkeypatch.setattr(backoff.random, "uniform", lambda a, b: seen.append((a, b)) or b)   # верхний край
    got = [backoff.backoff_delay(backoff.RECONNECT_S, n) for n in range(9)]
    assert [round(d, 6) for d in got] == [7.0, 14.0, 28.0, 56.0, 112.0, 224.0, 420.0, 420.0, 420.0], got
    assert set(seen) == {(0.6, 1.4)}, "разброс шага — не ± 40 %"
    assert backoff.backoff_delay((5, 15), 0, rnd=lambda: 0.0) == pytest.approx(3.0)
    assert backoff.backoff_delay((5, 15), -1, rnd=lambda: 0.5) == pytest.approx(5.0), "до таблицы — первый шаг"


def test_the_pair_and_guest_channels_share_one_table_and_the_link_keeps_its_own():
    """Пара и гость переподключались по двум одинаковым копиям таблицы; у
    канала линка шлюза таблица своя (реже на старте) и остаётся своей."""
    from awgbot.runtime import guestlink, linkclient
    from awgbot.runtime.pairlink import common as paircommon
    assert backoff.RECONNECT_S == (5, 10, 20, 40, 80, 160, 300)
    assert not hasattr(guestlink, "_BACKOFF") and not hasattr(paircommon, "_BACKOFF"), "копия таблицы вернулась"
    assert linkclient._BACKOFF == (5, 15, 45, 135, 300)


async def test_the_pair_and_guest_clients_wait_by_the_shared_table(monkeypatch):
    """Канал пары и гостевой канал между попытками ждут шаг общей таблицы с
    разбросом: попытка за попыткой — 5, 10, 20 … с, верхний край ± 40 %."""
    import asyncio
    import types
    from awgbot.runtime import guestlink
    from awgbot.runtime.pairlink import client as pairclient
    monkeypatch.setattr(backoff.random, "uniform", lambda a, b: b)
    for mod, cls in ((guestlink, guestlink.GuestClient), (pairclient, pairclient.PairClient)):
        waits: list[float] = []

        async def wait_for(aw, timeout, _w=waits):
            aw.close()
            _w.append(timeout)
            if len(_w) >= 3:
                raise asyncio.CancelledError
            raise asyncio.TimeoutError
        monkeypatch.setattr(mod.asyncio, "wait_for", wait_for)
        c = cls(types.SimpleNamespace(reserve_invite=lambda: {}, reserve_connect_address=lambda: ""))

        async def boom():
            raise OSError("нет связи")
        c._connect_once = boom
        with pytest.raises(asyncio.CancelledError):
            await c._run()
        assert [round(w, 6) for w in waits] == [7.0, 14.0, 28.0], (cls.__name__, waits)


# ── сторож: одна формула разброса ────────────────────────────────────────────

# несимметричные окна (не «± доля») и машина резерва (её такты переводятся отдельно)
_UNIFORM_ALLOWED = {
    "awgbot/util/backoff.py",
    "awgbot/domain/services/reserve/outbound_return.py",     # ± 20 % повтора забора домой — машина резерва
    "awgbot/domain/services/reserve/pult/primary.py",        # 1.0…1.4: проба пульта не раньше срока
}


def test_no_module_multiplies_by_its_own_random_uniform():
    root = Path(__file__).resolve().parents[2]
    pat = re.compile(r"\*\s*random\.uniform\(|random\.uniform\(\s*[A-Za-z_][\w.]*\s*\*\s*0\.6")
    bad = []
    for p in sorted((root / "awgbot").rglob("*.py")):
        rel = p.relative_to(root).as_posix()
        if rel in _UNIFORM_ALLOWED:
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                bad.append(f"{rel}:{n}: {line.strip()}")
    assert bad == [], "разброс сроков — через util/backoff.jitter / backoff_delay:\n" + "\n".join(bad)
