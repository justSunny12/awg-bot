"""Фикстуры эталонов экранов: заморозка часов, версии и хоста; фейки
conftest с исходным состоянием для сброса перед каждым снимком."""
from __future__ import annotations

import copy

import pytest

from awgbot.core import config
from awgbot.util import timeutil
from tests.conftest import restore_settings
from tests.screens import base


@pytest.fixture()
def fakes(fake_awg, fake_routing):
    """Фейки conftest с исходным состоянием: take() возвращает их к нему перед
    каждым снимком — иначе ключи и адреса (vpn://) зависели бы от того,
    сколько снимков прошло до этого в том же тесте."""
    return [(ns, copy.deepcopy(vars(ns))) for ns in (fake_awg, fake_routing)]


@pytest.fixture()
def frozen(monkeypatch):
    """Часы, версия и хост — константы каталога; имя хоста в шапке главной
    админа кэшируется на процесс — закрепляем его тоже."""
    import socket
    import time
    from awgbot.bot.texts import admin as admin_texts
    from awgbot.infra import hostmetrics
    monkeypatch.setattr(timeutil, "now", lambda: base.NOW)
    monkeypatch.setattr(time, "time", lambda: base.NOW.timestamp())
    monkeypatch.setattr(config, "INSTALLED_VERSION", base.VERSION)
    monkeypatch.setattr(socket, "gethostname", lambda: base.HOST)
    monkeypatch.setattr(admin_texts, "_HOSTNAME", base.HOST)
    monkeypatch.setattr(hostmetrics, "_is_pi", False)
    yield
    restore_settings()
