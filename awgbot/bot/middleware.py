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

from awgbot.core import access_cache, config
from awgbot.core.enums import ActivationStatus


class AccessMiddleware(BaseMiddleware):
    def __init__(self, db):
        self.db = db

    def _touch_tg_name(self, client, user: User):
        """Имя Telegram-аккаунта на профиле — для ссылок на человека в текстах.
        Пишем только при изменении: запись сбрасывает кэш ролей."""
        if client is None:
            return None
        name = (user.full_name or user.username or "").strip()[:128]
        uname = (user.username or "").strip()[:64]
        if name and (name != client.tg_name or uname != client.tg_username):
            from awgbot.util import timeutil
            self.db.update_client_fields(client.id, tg_name=name, tg_username=uname,
                                         tg_name_at=timeutil.now_iso())
            return self.db.get_client(client.id)
        return client

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

        # 1) Админ — полный доступ. Имя его Telegram-аккаунта тоже нужно (он
        #    бывает дарителем) — обновляем не чаще TTL кэша, как у остальных.
        if uid == config.ADMIN_ID:
            data["role"] = "admin"
            data["client"] = None
            if access_cache.get(uid) is None:
                self._touch_tg_name(self.db.get_client_by_tg(uid), user)
                access_cache.put(uid, "admin", None)
            return await handler(event, data)

        # 2) Кэш «кто это»: TTL короткий, любая запись в БД сбрасывает его.
        #    Без кэша каждый апдейт стоил 1–3 запроса синхронно в event loop.
        hit = access_cache.get(uid)
        if hit is not None and hit[0] != "stranger":
            role, client, _device = hit
            data["role"] = role
            data["client"] = client
            return await handler(event, data)
        if hit is None:
            cached_stranger = False
        else:
            cached_stranger = True                      # отрицательный кэш: флуд
            client = None                               # посторонних — 0 SQL

        # 3) Профиль из БД: владелец → client; гость (docs/guest-role.md) →
        #    invited с его собственным гостевым профилем в client
        client = None if cached_stranger else self.db.get_client_by_tg(uid)
        if (client is not None and not client.is_service
                and client.activation_status == ActivationStatus.ACTIVE):
            client = self._touch_tg_name(client, user)
            if client.is_guest:
                # имя гостя — из Telegram: при переносе прежних друзей его не
                # было, а средство узнать — только первое сообщение
                name = (user.first_name or user.username or "").strip()
                if name and client.name in ("Друг", ""):
                    self.db.update_client_fields(client.id, name=name[:64])
                    client = self.db.get_client(client.id)
                access_cache.put(uid, "invited", client)
                data["role"] = "invited"
                data["client"] = client
                return await handler(event, data)
            access_cache.put(uid, "client", client)
            data["role"] = "client"
            data["client"] = client
            return await handler(event, data)

        if not cached_stranger:
            access_cache.put(uid, "stranger", None)     # активация запишет в БД и сбросит

        # 5) Незнакомец: пропускаем только команды активации — /start (холодный
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
