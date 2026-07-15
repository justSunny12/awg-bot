"""Smoke: импортируется КАЖДЫЙ модуль пакета (+ tools, agent) без ошибок.

Дёшево ловит битые импорты, циклические зависимости и опечатки после рефактора
по всей кодовой базе разом.
"""
import importlib
import pkgutil

import pytest

pytestmark = pytest.mark.smoke


def _all_submodules():
    import awgbot
    mods = []
    for m in pkgutil.walk_packages(awgbot.__path__, prefix="awgbot."):
        mods.append(m.name)
    return mods


@pytest.mark.parametrize("modname", _all_submodules())
def test_import_every_awgbot_module(modname):
    importlib.import_module(modname)


def test_import_entrypoint_and_tools():
    importlib.import_module("awgbot.__main__")
    importlib.import_module("tools.manage_secrets")
    importlib.import_module("tools.restore_backup")


