"""Smoke: aiogram-роутеры существуют и собираются в Dispatcher (проверка проводки).

Замечание: роутер aiogram можно включить в диспетчер лишь раз (один родитель),
поэтому реальные синглтон-роутеры включаем РОВНО В ОДНОМ тесте, чтобы не портить
их глобальное состояние для остальных тестов.
"""
import pytest
from aiogram import Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage

from awgbot.bot.handlers import (admin, client, friend, guide,
                                 reply_commands)

pytestmark = pytest.mark.smoke

# Порядок включения — как в awgbot.runtime.main (reply_commands первым).
_HANDLER_MODULES = [reply_commands, admin, guide, friend, client]


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


def test_dispatcher_rejects_double_include_of_same_router():
    # инвариант aiogram (на свежем роутере, чтобы не трогать боевые синглтоны)
    dp = Dispatcher(storage=MemoryStorage())
    throwaway = Router(name="throwaway")
    dp.include_router(throwaway)
    with pytest.raises(RuntimeError):
        dp.include_router(throwaway)
