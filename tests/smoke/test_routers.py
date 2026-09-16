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
    подчёркиванием, обработчики — без; регистрация помощника — брак."""
    handlers = [h.callback for obs in (mod.router.message, mod.router.callback_query)
                for h in obs.handlers]
    leaked = [h.__name__ for h in handlers if h.__name__.startswith("_")]
    assert not leaked, leaked
    assert handlers, "в роутере нет ни одного обработчика"
