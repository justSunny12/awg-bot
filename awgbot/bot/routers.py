"""routers.py — один источник роутеров роли установки.

Запуск (runtime/main.py) и тесты (эталоны экранов гонят события через
настоящий диспетчер роли) собирают диспетчер отсюда — списков роутеров в
двух местах больше нет, и порядок включения живёт здесь один раз:

  paging → hide → cancel → [основной бот: reply_commands] → sections(br) →
  [роутеры роли по порядку] → stale

Роутеры только одной роли (admin, settings, client, gateway…) — модульные,
в процессе их включает один диспетчер; общие для обеих ролей (листание,
«Скрыть», «✖️ Отмена», общие разделы настроек, устаревшая кнопка) — фабрики:
в одном процессе тестов строятся оба диспетчера, а один Router в два не включить.
"""
from __future__ import annotations

from aiogram import Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage

from awgbot.bot.middleware import AccessMiddleware


def routers_for(role: str) -> list[Router]:
    """Роутеры роли установки («client» — основной бот, «gateway» — агент)
    в порядке включения. Импорты ленивые: модуль не тянет хендлеры при загрузке."""
    from awgbot.bot import paging, sections
    from awgbot.bot.handlers import hide, reply_commands, stale
    from awgbot.bot.roles import GATEWAY, MAIN
    br = GATEWAY if role == "gateway" else MAIN
    # общие разделы — раньше роутеров роли: у тех широкие фильтры (act == "do")
    head = [paging.make_router(), hide.make_router(), reply_commands.make_cancel_router()]
    shared = sections.make_router(br)
    if role == "gateway":
        from awgbot.bot.handlers import gateway as gateway_handlers

        async def _gw_main(message, services, role="", client=None):
            await gateway_handlers._panel(message, services)
        # ПОСЛЕДНИМ: кнопка старого меню
        return head + [shared, gateway_handlers.router, stale.make_router(_gw_main)]
    from awgbot.bot.handlers import admin, client, friend, guide, reply_commands, routing, settings
    from awgbot.bot.handlers.common import show_main_menu
    # reply_commands ПЕРВЫМ — до общих разделов: reply-«✖️ Отмена» бьёт раньше
    # FSM ввода (иначе записалась бы как значение); routing ДО client — у обоих
    # роль client, и FSM ввода адресов должен ловиться в routing, а не общим
    # message-хендлером клиента; stale ПОСЛЕДНИМ
    return head + [reply_commands.router, shared, admin.router, settings.router, guide.router,
                   friend.router, routing.router, client.router, stale.make_router(show_main_menu)]


def make_dispatcher(role: str, services, db, *, reattach: bool = False) -> Dispatcher:
    """Диспетчер роли: память FSM, сервисы в data, AccessMiddleware снаружи
    (outer — до фильтров роутеров: RoleFilter читает data['role'], который
    кладёт именно он) и роутеры роли по порядку. reattach — для тестов: модульный
    роутер, уже включённый в другой диспетчер, отцепляется и включается сюда."""
    dp = Dispatcher(storage=MemoryStorage())
    dp["services"] = services
    access = AccessMiddleware(db)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    for r in routers_for(role):
        if reattach:
            r._parent_router = None
        dp.include_router(r)
    return dp
