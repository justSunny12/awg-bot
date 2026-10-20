"""Фикстуры эталонов экранов: заморозка часов, версии и хоста; фейки
conftest с исходным состоянием для сброса перед каждым снимком."""
from __future__ import annotations

import copy
import os

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


def _hide_host_root(monkeypatch, fake_root):
    """Выданные шлюзовые стороны линков бот читает по прибитому пути
    /root/gw-<линк>.conf. Каталог хоста в снимки не попадает: на macOS его
    нет (ENOENT), на Linux он есть и закрыт (EACCES) — текст ошибки в карточке
    слота зависел бы от системы прогона. Чтения под /root идут в пустой
    временный каталог, ошибка называет исходный путь — эталон один на обе."""
    import builtins
    fake_root.mkdir(exist_ok=True)
    real_open = builtins.open

    def _open(file, *a, **kw):
        path = os.fspath(file) if isinstance(file, (str, os.PathLike)) else None
        if isinstance(path, str) and path.startswith("/root/"):
            try:
                return real_open(fake_root / path[len("/root/"):], *a, **kw)
            except OSError as e:
                e.filename = path
                raise
        return real_open(file, *a, **kw)
    monkeypatch.setattr(builtins, "open", _open)


@pytest.fixture()
def frozen(monkeypatch, tmp_path):
    """Часы, версия и хост — константы каталога; имя хоста в шапке главной
    админа кэшируется на процесс — закрепляем его тоже. Файл поколения
    AmneziaWG — свой, пустой: тесты переезда того же процесса пишут общий, и
    «gen1» в карточке сервера иначе зависел бы от порядка прогона."""
    import socket
    import time
    from awgbot.bot.texts import admin as admin_texts
    from awgbot.infra import awglock, hostmetrics
    monkeypatch.setattr(awglock, "STATE_PATH", tmp_path / "awg.state")
    monkeypatch.setattr(timeutil, "now", lambda: base.NOW)
    monkeypatch.setattr(time, "time", lambda: base.NOW.timestamp())
    monkeypatch.setattr(config, "INSTALLED_VERSION", base.VERSION)
    monkeypatch.setattr(socket, "gethostname", lambda: base.HOST)
    monkeypatch.setattr(admin_texts, "_HOSTNAME", base.HOST)
    monkeypatch.setattr(hostmetrics, "_is_pi", False)
    _hide_host_root(monkeypatch, tmp_path / "root")
    yield
    restore_settings()
