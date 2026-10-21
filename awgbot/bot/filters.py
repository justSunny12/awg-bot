"""filters.py — фильтр маршрутизации по роли (её проставил middleware)."""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject


class RoleFilter(BaseFilter):
    """Пропускает событие, если data['role'] ∈ разрешённых. Роль кладёт
    AccessMiddleware; фильтры aiogram получают её как kwarg."""

    def __init__(self, *roles: str):
        self.roles = set(roles)

    async def __call__(self, event: TelegramObject, role: str = "", **kw) -> bool:
        return role in self.roles


class PeerAdminFilter(BaseFilter):
    """Кнопки экрана роли админа второго сервера: сама роль — или админ этого
    сервера, когда он же админ второго (экран роли — раздел его «🛟 Резерв»)."""

    async def __call__(self, event: TelegramObject, role: str = "", services=None, **kw) -> bool:
        if role == "peer_admin":
            return True
        if role == "admin" and services is not None:
            # тот же короткий кэш, что у middleware: SELECT на каждое сообщение админа ни к чему
            try:
                from awgbot.core import access_cache, config
                from awgbot.bot.middleware import _PEER_ADMIN_SLOT
                hit = access_cache.get(_PEER_ADMIN_SLOT)
                if hit is not None:
                    return int(hit[1] or 0) == int(config.ADMIN_ID) and int(hit[1] or 0) > 0
                return bool(services.reserve_self_peer_admin())
            except Exception:                                 # noqa: BLE001
                return False
        return False


__all__ = ["RoleFilter", "PeerAdminFilter"]
