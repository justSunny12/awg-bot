"""Smoke: aiogram-роутеры существуют и собираются в Dispatcher (проверка проводки).

Замечание: роутер aiogram можно включить в диспетчер лишь раз (один родитель),
поэтому реальные синглтон-роутеры включаем РОВНО В ОДНОМ тесте, чтобы не портить
их глобальное состояние для остальных тестов.
"""
import pytest
from aiogram import Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage

from awgbot.bot.handlers import (admin, client, friend, guide,
                                 reply_commands, routing, settings)

pytestmark = pytest.mark.smoke

# Порядок включения — как в awgbot.runtime.main (reply_commands первым; routing
# ДО client, иначе FSM ввода адресов перехватит общий message-хендлер клиента).
_HANDLER_MODULES = [reply_commands, admin, settings, guide, friend, routing, client]


@pytest.mark.parametrize("mod", _HANDLER_MODULES, ids=lambda m: m.__name__.split(".")[-1])
def test_each_handler_exposes_named_router(mod):
    assert isinstance(mod.router, Router)
    assert mod.router.name                          # у каждого осмысленное имя


def test_routers_are_distinct_objects():
    routers = [m.router for m in _HANDLER_MODULES]
    assert len({id(r) for r in routers}) == len(routers)


def test_real_routers_assemble_into_dispatcher():
    # единственный потребитель реальных роутеров: включаем как main, ровно один раз
    dp = Dispatcher(storage=MemoryStorage())
    for mod in _HANDLER_MODULES:
        dp.include_router(mod.router)
    assert len(list(dp.sub_routers)) == len(_HANDLER_MODULES)


@pytest.mark.parametrize("mod", _HANDLER_MODULES, ids=lambda m: m.__name__.split(".")[-1])
def test_no_helper_is_registered_as_a_handler(mod):
    """Декоратор, вставший над вспомогательной функцией, вешает на кнопку
    помощник с чужой сигнатурой — aiogram зовёт его с cb/services и падает.
    Ровно так «Мои устройства» у клиента упали в v2.20.0. Помощники — с
    подчёркиванием, обработчики — без; регистрация помощника — брак.
    Роутер может быть пакетом из подроутеров (admin) — обходим всю цепочку."""
    handlers = [h.callback for sub in mod.router.chain_tail
                for obs in (sub.message, sub.callback_query)
                for h in obs.handlers]
    leaked = [h.__name__ for h in handlers if h.__name__.startswith("_")]
    assert not leaked, leaked
    assert handlers, "в роутере нет ни одного обработчика"


# ── один источник роутеров роли ──────────────────────────────────────────────

def test_routers_for_lists_both_roles_in_the_documented_order():
    """routers.py — единственное место с порядком включения: листание и
    «Скрыть» первыми, устаревшая кнопка последней; у основного бота
    reply_commands раньше FSM, routing раньше client."""
    from awgbot.bot.routers import routers_for
    main_names = [r.name for r in routers_for("client")]
    gw_names = [r.name for r in routers_for("gateway")]
    assert main_names[:2] == ["paging", "hide"] and main_names[-1] == "stale", main_names
    assert gw_names[:2] == ["paging", "hide"] and gw_names[-1] == "stale", gw_names
    assert main_names.index("reply_commands") < main_names.index("admin"), main_names
    assert main_names.index("routing") < main_names.index("client"), main_names
    assert "gateway" in gw_names and "admin" not in gw_names, gw_names


def test_both_dispatchers_build_in_one_process(services, monkeypatch):
    """Эталоны экранов гонят события через настоящие диспетчеры обеих ролей в
    одном процессе: общие роутеры — фабрики, модульные — переподключаются
    (reattach) и получают фильтр роли через AccessMiddleware."""
    from awgbot.bot.routers import make_dispatcher
    main_dp = make_dispatcher("client", services, services.db, reattach=True)
    gw_dp = make_dispatcher("gateway", services, services.db, reattach=True)
    assert main_dp["services"] is services and gw_dp["services"] is services
    assert [r.name for r in main_dp.sub_routers][-1] == "stale"
    assert [r.name for r in gw_dp.sub_routers] == ["paging", "hide", "gateway", "stale"]
    assert main_dp.message.outer_middleware and main_dp.callback_query.outer_middleware
    # повторная сборка той же роли в том же процессе — снова через reattach
    again = make_dispatcher("gateway", services, services.db, reattach=True)
    assert [r.name for r in again.sub_routers] == ["paging", "hide", "gateway", "stale"]
