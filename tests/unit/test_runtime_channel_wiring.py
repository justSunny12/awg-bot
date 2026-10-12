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
    src = (ROOT / "main" / "client.py").read_text(encoding="utf-8")
    assert src.index("set_on_applied(") < src.index("scheduler.start()") < src.index("linkserver.ensure(services)")
    assert src.index("gateway_units_migrate") < src.index("scheduler.start()"), "планировщик раньше миграции юнитов слотов"
    assert src.count("set_on_applied(") == 1 and src.count("set_on_installed(") == 1


def test_liveness_tick_is_light_and_the_monitor_tick_delivers():
    """Такт живости (30 с): слушатели, мёртвые сессии, роль; полная сверка
    доставки — на тике монитора и каждый четвёртый такт, отдельной задачей."""
    src = (ROOT / "scheduler" / "client.py").read_text(encoding="utf-8")
    assert "linkserver.liveness_tick(services)" in src and "linkserver.monitor_tick(services)" in src
    assert "await linkserver.ensure(services)" not in src, "такт снова зовёт полную доставку синхронно"
    ls = (ROOT / "linkserver" / "__init__.py").read_text(encoding="utf-8")
    body = ls.split("async def liveness_tick", 1)[1].split("async def monitor_tick", 1)[0]
    assert "sweep_dead()" in body and "send_roles()" in body and "deliver_soon()" in body
    assert "await _server.deliver_all()" not in body


def test_the_admin_resolver_is_ensured_on_start_and_watched_on_the_monitor_tick():
    """Живые хосты установщик заново не проходят: без вызова на старте
    резолвер админа на них не появился бы никогда; без такта монитора
    замолчавший слот не выпал бы из записей, а упавший резолвер — без слова."""
    main = (ROOT / "main" / "client.py").read_text(encoding="utf-8")
    assert "services.admin_dns_ensure" in main
    assert main.index("services.resolver_ensure_dropin") < main.index("services.admin_dns_ensure"), \
        "резолвер админа ставится раньше, чем подтянут override резолвера клиентов"
    sched = (ROOT / "scheduler" / "client.py").read_text(encoding="utf-8")
    body = sched.split("services.admin_dns_health_tick", 1)
    assert len(body) == 2 and "send_notifications(bot, adm_notes)" in body[1].split("except", 1)[0], \
        "уведомления монитора резолвера админа никуда не уходят"


def test_the_update_hook_and_the_window_are_wired_before_the_channel_starts():
    """Пересчёт цели по событиям канала уведомляет крючком main: без него
    обновлённый шлюз открыл бы серверу цель молча (до плановой проверки — до
    месяца). Окно ожидания после старта открывается до планировщика: иначе
    стартовая проверка решила бы по старым версиям."""
    main = (ROOT / "main" / "client.py").read_text(encoding="utf-8")
    assert main.count("set_on_update(") == 1
    assert main.index("set_on_update(") < main.index("scheduler.start()") < main.index("linkserver.ensure(services)")
    assert main.index("services.update_hold_start()") < main.index("scheduler.start()")
    assert "notify_update_available(bot, services, nxt)" in main.split("set_on_update(", 1)[0][-600:]
    gw = (ROOT / "main" / "gateway.py").read_text(encoding="utf-8")
    assert gw.count("linkclient.set_on_update(") == 1
    assert gw.index("services.update_hold_start()") < gw.index("setup_gateway_scheduler(services, bot)")
    assert gw.index("linkclient.set_on_update(") < gw.index("linkclient.ensure(services)")


def test_the_liveness_tick_keeps_seen_true_and_regates():
    ls = (ROOT / "linkserver" / "__init__.py").read_text(encoding="utf-8")
    body = ls.split("async def liveness_tick", 1)[1].split("async def monitor_tick", 1)[0]
    assert "gwlink_seen_touch" in body and "update_regate(services, force=n % 20 == 0)" in body
    lc = (ROOT / "linkclient" / "__init__.py").read_text(encoding="utf-8")
    tick = lc.split("async def on_tick", 1)[1].split("\nasync def ", 1)[0]
    assert "update_regate(services, force=False)" in tick, "конец окна после старта у агента — без события"
