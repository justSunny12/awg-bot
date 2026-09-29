"""
Крючки канала линка и такты планировщика — по исходнику: слушатель и
планировщик поднимаются до `_after_start`, и крючки обязаны стоять раньше.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2] / "awgbot" / "runtime"


def test_channel_hooks_are_set_before_the_scheduler_and_the_listener_start():
    """Агент, подключившийся во время старта, шлёт applied/installed сразу;
    без крючка ack уходит, повтора не будет — итог применения и «шлюз
    настроен» терялись бы, файл с ключом оставался в чате."""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert src.index("set_on_applied(") < src.index("scheduler.start()") < src.index("linkserver.ensure(services)")
    assert src.index("gateway_units_migrate") < src.index("scheduler.start()"), "планировщик раньше миграции юнитов слотов"
    assert src.count("set_on_applied(") == 1 and src.count("set_on_installed(") == 1


def test_liveness_tick_is_light_and_the_monitor_tick_delivers():
    """Такт живости (30 с): слушатели, мёртвые сессии, роль; полная сверка
    доставки — на тике монитора и каждый четвёртый такт, отдельной задачей."""
    src = (ROOT / "scheduler.py").read_text(encoding="utf-8")
    assert "linkserver.liveness_tick(services)" in src and "linkserver.monitor_tick(services)" in src
    assert "await linkserver.ensure(services)" not in src, "такт снова зовёт полную доставку синхронно"
    ls = (ROOT / "linkserver.py").read_text(encoding="utf-8")
    body = ls.split("async def liveness_tick", 1)[1].split("async def monitor_tick", 1)[0]
    assert "sweep_dead()" in body and "send_roles()" in body and "deliver_soon()" in body
    assert "await _server.deliver_all()" not in body
