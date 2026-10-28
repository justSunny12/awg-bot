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


def test_import_every_awgbot_module():
    """Все модули одним тестом; в сообщении — каждый неимпортируемый модуль
    с причиной, а не первый."""
    bad = []
    for modname in _all_submodules():
        try:
            importlib.import_module(modname)
        except Exception as e:                            # noqa: BLE001
            bad.append(f"{modname}: {type(e).__name__}: {e}")
    assert not bad, "модули не импортируются:\n" + "\n".join(bad)


def test_import_entrypoint_and_tools():
    importlib.import_module("awgbot.__main__")
    importlib.import_module("tools.restore_backup")


