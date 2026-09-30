"""Unit: замер очереди пула потоков после старта (rt-18) — только журнал."""
import logging

import pytest

from awgbot.runtime import poolprobe

pytestmark = pytest.mark.unit


async def test_measure_returns_stats_without_touching_anything():
    st = await poolprobe.measure(duration=0.05, every=0.01)
    assert st["samples"] >= 1 and st["max"] >= 0.0 and st["slow"] == 0


async def test_run_logs_one_summary_line(caplog, monkeypatch):
    monkeypatch.setattr(poolprobe, "DURATION", 0.03)
    monkeypatch.setattr(poolprobe, "EVERY", 0.01)

    async def _measure(duration=0.03, every=0.01):
        return {"samples": 3, "max": 0.2, "avg": 0.1, "slow": 0}
    monkeypatch.setattr(poolprobe, "measure", _measure)
    with caplog.at_level(logging.INFO, logger="awgbot.poolprobe"):
        await poolprobe.run("gateway: ")
    assert any("gateway: пул потоков за" in r.getMessage() and "0 из 3" in r.getMessage() for r in caplog.records)
