"""Агент шлюза знает своего бота сам: после getMe на старте (run_gateway) он
кладёт username и имя в services, а снимок канала (gw_snapshot) везёт их на
ВПС полем agent_bot. Так карточка слота на сервере ведёт в чат бота шлюза
ссылкой, даже если токена агента на сервере нет.

Сборка роли настоящая (run_gateway) до места, где регистрируется запасной
канал писем; дальше сборка останавливается. Telegram подменён двойником
aiogram.Bot в модуле main; всё, что снимок читает с хоста, — подменами."""
from __future__ import annotations

import types

import pytest
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.methods import GetMe

from awgbot.bot import notifier
from awgbot.core import config
from awgbot.domain import gateway as gateway_mod
from awgbot.domain import gwsnapshot
from awgbot.infra import awglock, gwguard
from awgbot.runtime import main, preflight


class _Stop(Exception):
    """Сборка дошла до регистрации запасного канала — дальше не идём."""


class _Watcher:
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass


class _Agent:
    """Что агент получил от Telegram на старте и какие сервисы собрал.

    me — ответ getMe (username, first_name) или исключение, которым getMe
    падает; services — экземпляр GatewayServices, собранный run_gateway."""

    def __init__(self, monkeypatch, tmp_path):
        self.me: object = ("naspi_gw_bot", "Шлюз квартиры")
        self.services = None
        self.getme_calls = 0
        agent = self

        class _Bot:
            def __init__(self, token, *a, **k):
                self.session = None

            async def get_me(self):
                agent.getme_calls += 1
                if isinstance(agent.me, BaseException):
                    raise agent.me
                username, first = agent.me
                return types.SimpleNamespace(username=username, first_name=first, id=42, is_bot=True)

        class _Services(gateway_mod.GatewayServices):
            def __init__(self, db):
                super().__init__(db)
                agent.services = self

        def _stop(fn):
            raise _Stop

        # сборка цепляет модульные роутеры к своему диспетчеру; после теста — отцепить
        from awgbot.bot import paging
        from awgbot.bot.handlers import gateway as gateway_handlers
        for r in (paging.router, gateway_handlers.router):
            monkeypatch.setattr(r, "_parent_router", None)
        monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "gw.db"))
        monkeypatch.setattr(config, "BOT_TOKEN", "42:DUMMY")
        monkeypatch.setattr(main, "Bot", _Bot)
        monkeypatch.setattr(main, "ConfWatcher", _Watcher)
        monkeypatch.setattr(gateway_mod, "GatewayServices", _Services)
        monkeypatch.setattr(notifier, "set_email_fallback", _stop)
        # снимок — только из подмен: юнита обвязки и поставки на машине теста нет
        monkeypatch.setattr(gwguard, "unit_env", lambda k: "")
        monkeypatch.setattr(awglock, "generation", lambda: 1)
        monkeypatch.setattr(config, "GW_LINK_CONF", str(tmp_path / "нет.conf"))
        monkeypatch.setattr(gwsnapshot, "boot_id", lambda: "b" * 36)

    async def start(self):
        with pytest.raises(_Stop):
            await main.run_gateway()
        assert self.services is not None, "run_gateway не собрал сервисы агента"
        assert self.getme_calls == 1, "getMe на старте агента должен уйти ровно один раз"
        return self.services

    def snapshot(self) -> dict:
        self.services._guard_info = {"sets": {}, "chains": {"input", "ssh_in"},
                                     "masq_ifaces": set(), "ssh_ports": {}}
        return self.services.gw_snapshot()


@pytest.fixture()
def agent(monkeypatch, tmp_path):
    a = _Agent(monkeypatch, tmp_path)
    yield a
    if a.services is not None:
        a.services.db.close()


async def test_agent_puts_its_own_bot_into_the_channel_snapshot(agent):
    """Сервер ведёт в чат бота шлюза ссылкой по тому, что прислал агент. Не
    положи агент ответ getMe в снимок — у слота без токена на сервере ссылки
    нет вовсе, и человек ищет бота шлюза в Telegram по памяти."""
    svc = await agent.start()
    assert (svc.bot_username, svc.bot_name) == ("naspi_gw_bot", "Шлюз квартиры"), \
        "ответ getMe не сохранён в сервисах агента"
    assert agent.snapshot()["agent_bot"] == {"username": "naspi_gw_bot", "name": "Шлюз квартиры"}, \
        "снимок канала не везёт бота шлюза"


async def test_bot_without_first_name_is_named_by_its_username(agent):
    """Пустое имя профиля — подпись ссылки username'ом, а не пустая ссылка
    «<a …></a>», которую в карточке не видно и не нажать."""
    agent.me = ("naspi_gw_bot", "")
    await agent.start()
    assert agent.snapshot()["agent_bot"] == {"username": "naspi_gw_bot", "name": "naspi_gw_bot"}


async def test_failed_getme_leaves_the_field_empty_not_invented(agent):
    """Сети на старте не было (Telegram ходит через линк, а он ещё не
    поднялся): агент стартует дальше, а в снимке — пустые строки. Выдуманное
    имя увело бы ссылку с сервера в чужой чат."""
    agent.me = RuntimeError("сеть не готова")
    svc = await agent.start()
    # у сервисов агента своих умолчаний нет: без getMe атрибута может не быть вовсе
    assert (getattr(svc, "bot_username", ""), getattr(svc, "bot_name", "")) == ("", ""), \
        "без ответа getMe в сервисах что-то появилось"
    assert agent.snapshot()["agent_bot"] == {"username": "", "name": ""}, \
        "без getMe поле обязано быть пустым, а не выдуманным и не пропавшим"


async def test_bot_without_username_gives_empty_fields_not_none(agent):
    """Telegram не прислал username (двойник покрывает None): в снимке строки,
    а не None — сервер сравнивает и хранит их как строки."""
    agent.me = (None, None)
    await agent.start()
    assert agent.snapshot()["agent_bot"] == {"username": "", "name": ""}


async def test_revoked_token_still_stops_the_agent(agent):
    """Сохранение имени бота не проглотило отказ Telegram по токену: агент с
    отозванным токеном обязан остановиться с понятной причиной, а не молча
    работать без чата."""
    agent.me = TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")
    with pytest.raises(preflight.PreflightError):
        await main.run_gateway()
    assert getattr(agent.services, "bot_username", "") == "", "отказ по токену оставил имя бота"
