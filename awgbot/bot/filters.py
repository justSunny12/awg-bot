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


__all__ = ["RoleFilter"]
