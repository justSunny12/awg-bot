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
    """Агент, подключившийся во время старта, шлёт installed сразу; без
    крючка повтора не будет — «шлюз настроен» терялось бы, файл с ключом
    оставался в чате. Крючка итога применения файла (applied) больше нет."""
    src = (ROOT / "main" / "client.py").read_text(encoding="utf-8")
    assert src.index("set_on_installed(") < src.index("scheduler.start()") < src.index("linkserver.ensure(services)")
    assert src.index("gateway_units_migrate") < src.index("scheduler.start()"), "планировщик раньше миграции юнитов слотов"
    assert src.count("set_on_applied(") == 0 and src.count("set_on_installed(") == 1


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


def test_no_update_window_is_left_in_either_role():
    """Окна ожидания после старта и пересчёта цели по версиям другой стороны
    нет ни у одной роли (порядок — по hello, правило «ждать факта»): прежних
    имён в исходнике быть не должно. Порядок крючков, слушателя и старта
    набора, такт живости и задание набора — прогоном настоящей сборки
    (test_review_fixes_main_start)."""
    main = (ROOT / "main" / "client.py").read_text(encoding="utf-8")
    gw = (ROOT / "main" / "gateway.py").read_text(encoding="utf-8")
    for src in (main, gw):
        assert "update_hold_start" not in src and "set_on_update(" not in src
    assert "services.upd_legacy_drop()" in gw
    ls = (ROOT / "linkserver" / "__init__.py").read_text(encoding="utf-8")
    lc = (ROOT / "linkclient" / "__init__.py").read_text(encoding="utf-8")
    assert "update_regate" not in ls and "update_regate" not in lc
