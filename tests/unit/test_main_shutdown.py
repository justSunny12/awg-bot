"""Штатное завершение процесса (runtime/main: client.py `main`, gateway.py
`run_gateway`, common.py `shutdown_steps`): в `finally` входов каналы
останавливаются в порядке, обратном подъёму, — у основного бота поллинг,
ответчик и зонды кворума, гостевой канал, локальный канал гостя, канал пары,
канал шлюзов; у агента — зонды кворума, гостевой канал к резерву, канал
конфигурации; каждый шаг с пределом, зависший или упавший не мешает остальным.

Цена ошибки: остановка юнита (SIGTERM) рвёт сокеты каналов без close — у
второй стороны полуоткрытые сессии до keepalive, ответчик кворума на снятом
интерфейсе; одна зависшая остановка (wait_closed без второй стороны) держит
юнит до TimeoutStopSec, и база закрывается SIGKILL.
"""
from __future__ import annotations

import asyncio

import pytest

from awgbot.runtime import guestlink, linkclient, linkserver, pairlink, pollctl, quorum
from awgbot.runtime.main import common
from awgbot.runtime.pairlink import guest as pair_guest
from awgbot.runtime.pairlink import guestctl
from tests.unit.test_gateway_token_start import _Start
from tests.unit.test_review_fixes_main_start import _Client

pytestmark = pytest.mark.unit


# ── помощник ─────────────────────────────────────────────────────────────────

async def test_steps_run_in_order_and_a_hung_or_failed_step_does_not_stop_the_rest(monkeypatch):
    seen: list[str] = []

    async def ok():
        seen.append("ok")

    async def hang():
        seen.append("hang")
        await asyncio.Event().wait()

    async def boom():
        seen.append("boom")
        raise RuntimeError("сокет уже снят")

    failed = await common.shutdown_steps([("первый", ok), ("висит", hang), ("падает", boom), ("последний", ok)],
                                         timeout=0.05)
    assert seen == ["ok", "hang", "boom", "ok"], "зависший или упавший шаг остановил остальные"
    assert failed == ["висит", "падает"]


def test_the_step_limit_is_short():
    assert 0 < common.SHUTDOWN_STEP_S <= 5


# ── входы ролей ──────────────────────────────────────────────────────────────

def _record(monkeypatch, module, name: str, seen: list, *, hang: bool = False):
    async def fn():
        seen.append(name)
        if hang:
            await asyncio.Event().wait()
    monkeypatch.setattr(module, "shutdown", fn)


@pytest.fixture()
def quick(monkeypatch):
    monkeypatch.setattr(common, "SHUTDOWN_STEP_S", 0.05)


async def test_the_main_bot_stops_every_channel_in_reverse_order(monkeypatch, tmp_path, fake_awg, fake_routing, quick):
    st = _Client(monkeypatch, tmp_path)
    seen: list[str] = []

    async def close(self):
        seen.append("pollctl")
    monkeypatch.setattr(pollctl.PollControl, "close", close)
    _record(monkeypatch, quorum, "quorum", seen)
    _record(monkeypatch, pair_guest, "guest", seen, hang=True)      # сокет в снятом пространстве — висит
    _record(monkeypatch, guestctl, "guestctl", seen)
    _record(monkeypatch, pairlink, "pairlink", seen)
    _record(monkeypatch, linkserver, "linkserver", seen)
    await st.run()
    assert seen == ["pollctl", "quorum", "guest", "guestctl", "pairlink", "linkserver"], \
        f"остановка каналов не в обратном порядке или не до конца: {seen}"


async def test_the_gateway_agent_stops_the_quorum_probes_the_guest_channel_and_the_link(monkeypatch, tmp_path, quick):
    st = _Start(monkeypatch, tmp_path)
    seen: list[str] = []
    _record(monkeypatch, quorum, "quorum", seen)
    _record(monkeypatch, guestlink, "guestlink", seen, hang=True)
    _record(monkeypatch, linkclient, "linkclient", seen)
    await st.run_to_polling()
    assert seen == ["quorum", "guestlink", "linkclient"], \
        f"агент: остановка каналов не в обратном порядке или не до конца: {seen}"
