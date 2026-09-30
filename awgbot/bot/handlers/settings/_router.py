"""Роутер раздела настроек — один на все модули пакета; порядок регистрации
обработчиков задаёт порядок импорта модулей в __init__ (как раньше — порядок в файле)."""
from aiogram import Router

from awgbot.bot.filters import RoleFilter

router = Router(name="settings")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))
