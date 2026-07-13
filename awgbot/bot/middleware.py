"""
middleware.py — охрана на входе. Бот реагирует только на whitelist:
админ (из конфига) + активные клиенты (из БД). Одно место вместо проверок
в каждом хендлере.

Особый случай: /start {invite_code} и /code от НЕизвестного пропускаем — иначе
активация невозможна (клиента ещё нет в whitelist). Всё прочее от чужих —
молчаливый дроп (return без вызова хендлера).

Роль и запись клиента прокидываются в data: data["role"], data["client"].
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject, User

from awgbot.core import config
from awgbot.core.enums import ActivationStatus


class AccessMiddleware(BaseMiddleware):
    def __init__(self, db):
        self.db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return None                                   # нет пользователя — дроп

        uid = user.id

        # 1) Админ — полный доступ
        if uid == config.ADMIN_ID:
            data["role"] = "admin"
            data["client"] = None
            return await handler(event, data)

        # 2) Активный клиент из БД (быстрый индексный read, безопасно из loop)
        client = self.db.get_client_by_tg(uid)
        if (client is not None and not client.is_service
                and client.activation_status == ActivationStatus.ACTIVE):
            data["role"] = "client"
            data["client"] = client
            return await handler(event, data)

        # 3) Друг (invited): управляет ОДНИМ гостевым устройством. Кладём в data
        #    и устройство, и клиента-хозяина (для показа его подписки).
        fdev = self.db.get_device_by_friend_tg(uid)
        if fdev is not None:
            data["role"] = "invited"
            data["client"] = self.db.get_client(fdev.client_id)  # хозяин
            data["device"] = fdev
            return await handler(event, data)

        # 4) Незнакомец: пропускаем только команды активации — /start (холодный
        #    вход или deep-link с кодом) и /code {код}. Любой другой текст —
        #    молчание (не реагируем на случайные сообщения посторонних).
        if isinstance(event, Message) and event.text:
            cmd = event.text.split(maxsplit=1)[0]
            cmd = cmd.split("@", 1)[0]        # /start@BotName → /start (нек. клиенты)
            if cmd in ("/start", "/code"):
                data["role"] = "activation"
                data["client"] = None
                return await handler(event, data)

        # 5) Всё остальное от чужих — молчание
        return None


__all__ = ["AccessMiddleware"]
