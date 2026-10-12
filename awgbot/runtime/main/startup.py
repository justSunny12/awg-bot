"""startup.py — часть пакета main (см. __init__): стартовые уведомления и финишеры обеих
ролей — меню команд, переезд профилей, свой DNS, реконсиляция, итог обновления, имя бота,
перезагрузка хоста."""

from __future__ import annotations

import asyncio
import hashlib

from aiogram import Bot

from awgbot.core import config
from awgbot.infra import awg
from awgbot.infra.db import Database
from awgbot.domain.services import Services
from awgbot.runtime import main as pkg          # подменяемое тестами — через пакет

from .common import log


async def _menu_command(bot) -> None:
    """Меню команд Telegram — одна команда «Меню» (/start) у обеих ролей.
    Сеть на старте может быть не готова — отказ не фатален."""
    from aiogram.types import BotCommand
    try:
        await bot.set_my_commands([BotCommand(command="start", description="Меню")])
    except Exception as e:                               # noqa: BLE001
        log.warning("меню команд не выставлено: %s", e)

async def _notify_migration_needed(bot: Bot, services: Services) -> None:
    """Сообщить админу, что нужен переезд профилей. Условия: манифест поставки
    новее применённого поколения, рычаг переезда настроен (второй интерфейс
    поднят установщиком) и переезд ещё не идёт."""
    from awgbot.bot import keyboards as kb
    from awgbot.bot import texts
    from awgbot.infra import awglock
    if not awglock.needs_migration():
        return
    if await asyncio.to_thread(services.migration_running):
        return
    if not await asyncio.to_thread(services.migration_available):
        # Рычага нет: второй интерфейс не поднят (установщик его не поднимал,
        # например пока шёл другой переезд). Предлагаем поднять кнопкой — иначе
        # тупик: интерфейс заводит только обновление, а оно уже прошло.
        await pkg.notify_one(bot, config.ADMIN_ID, texts.migration_generation_pending(),
                         reply_markup=kb.migration_generation_pending())
        return
    await pkg.notify_one(bot, config.ADMIN_ID,
                     texts.migration_needed(awglock.generation(), awglock.applied_generation()),
                     reply_markup=kb.migration_needed())


async def _notify_private_dns_offer(bot: Bot, services: Services) -> None:
    """Предложить свой DNS-резолвер: адрес в конфигах публичный, админ ещё не
    решал. Три кнопки — три решения, любое гасит инфобокс."""
    from awgbot.bot import keyboards as kb
    from awgbot.bot import texts
    if not await asyncio.to_thread(services.private_dns_offer_due):
        return
    info = await asyncio.to_thread(services.private_dns_info)
    await pkg.notify_one(bot, config.ADMIN_ID, texts.private_dns_offer(info["target"]),
                     reply_markup=kb.private_dns_offer_kb())


async def do_reconcile(services: Services, bot: Bot) -> None:
    """Реконсиляция состава пиров + рассылка уведомлений (вызов из вотчдога и старта).
    Внешнее изменение файлов могло затронуть и [Interface] — сбрасываем кэш
    серверных параметров (следующее чтение возьмёт живые значения)."""
    try:
        awg.invalidate_server_params()
        notifs = await asyncio.to_thread(services.reconcile_peers)
        await pkg.send_notifications(bot, notifs)
    except Exception as e:                               # noqa: BLE001
        log.warning("reconcile_peers: %s", e)


async def report_update_result(bot, services) -> None:
    """Финишер self-update: убрать «дождись», отчитаться, ЕДИНСТВЕННАЯ живая
    кнопка «В меню».

    Обновление через несколько ступеней даёт цепочку рестартов, и финишер
    приходит после каждого. Кнопку прежнего снимаем при отправке следующего:
    остаться она должна только на последнем — иначе в чате столько же живых
    «В меню», сколько было ступеней, и все ведут в одно место. Тексты при этом
    не трогаем: сама история «какая ступень чем закончилась» ценна.
    """
    wait = await asyncio.to_thread(services.pop_update_wait)
    note = await asyncio.to_thread(services.confirm_applied_update)
    if wait is not None:                                 # прибрать «дождись» всегда
        try:
            await bot.delete_message(chat_id=wait[0], message_id=wait[1])
        except Exception:                               # noqa: BLE001
            pass
    if note is None:
        return
    # кнопки у ВСЕХ прошлых окон обновления — долой (история, не один id:
    # между финишерами бывают «не применилось» и «не удалось» с той же кнопкой)
    from awgbot.bot.handlers.common import dismiss_update_reports
    await dismiss_update_reports(bot, services)
    sent = await pkg.notify_one(bot, note.tg_id, note.text,
                            reply_markup=note.reply_markup)
    if sent is not None:
        await asyncio.to_thread(services.remember_update_report,
                                sent.chat.id, sent.message_id)


async def _sync_bot_identity(bot: Bot, db: Database) -> bool:
    """Публичное имя/описания бота — из conf/bot_identity.yaml (маскирующие
    формулировки, ничего не должно выдавать назначение бота стороннему
    наблюдателю профиля). Правки, вбитые вручную в BotFather, переживут только
    до следующего рестарта — дальше их перетрёт этот блок.

    Меню команд (кнопка «/») НЕ регистрируем и явно СТИРАЕМ: раньше сюда уже
    отправлялся /code, Bot API хранит это до явной перезаписи.

    Отпечаток (имя, описания, версия) — в state: совпал — ни одного запроса к
    Bot API (раньше каждый старт стоил три-четыре, а set_my_name Telegram
    ещё и рейт-лимитит). Возвращает True, если запросы делались."""
    ident = hashlib.sha256("|".join((config.BOT_NAME or "", config.BOT_DESCRIPTION or "",
                                     config.BOT_SHORT_DESCRIPTION or "",
                                     config.INSTALLED_VERSION)).encode()).hexdigest()[:16]
    if db.get_state("bot_identity") == ident:
        return False
    if config.BOT_NAME and (await bot.get_my_name()).name != config.BOT_NAME:
        await bot.set_my_name(config.BOT_NAME)
    if config.BOT_DESCRIPTION and \
            (await bot.get_my_description()).description != config.BOT_DESCRIPTION:
        await bot.set_my_description(config.BOT_DESCRIPTION)
    if config.BOT_SHORT_DESCRIPTION and \
            (await bot.get_my_short_description()).short_description != config.BOT_SHORT_DESCRIPTION:
        await bot.set_my_short_description(config.BOT_SHORT_DESCRIPTION)
    await bot.delete_my_commands()
    db.set_state("bot_identity", ident)
    return True


async def _announce_reboot(bot, db, who: str) -> None:
    """«Хост перезагружен, запуск успешен» — после всех проверок старта. Только
    когда хост действительно перезагружался (boot_id сменился), не на каждый
    рестарт сервиса."""
    from awgbot.runtime import hostboot
    try:
        if not await asyncio.to_thread(hostboot.reboot_detected, db):
            return
        import socket
        from awgbot.bot.notifier import notify_one
        from awgbot.bot import texts
        await notify_one(bot, config.ADMIN_ID, texts.host_rebooted(socket.gethostname(), who))
    except Exception as e:                               # noqa: BLE001
        log.warning("announce_reboot: %s", e)
