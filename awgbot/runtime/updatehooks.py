"""updatehooks.py — пересчёт цели обновления по событиям канала, общий для ролей.

Слушатель канала на ВПС (версии агентов из hello и снимков) и клиент канала
на шлюзе (версия сервера в `role`) зовут один и тот же пересчёт без сети
(domain/selfupdate/cache.py) и, если открылась цель новее объявленной, — крючок
уведомления, который ставит main. Одна реализация на обе роли: правка
обработки ошибок или окна ожидания не может разойтись между ними.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class UpdateHook:
    """Крючок уведомления о версии (async fn(release)) и пересчёт по нему."""

    def __init__(self) -> None:
        self.fn = None

    async def regate(self, services, *, force: bool = True) -> None:
        """Пересчёт цели обновления без сети и уведомление крючком, если появилась
        цель новее объявленной. Исключение — в журнал здесь же: ни сессия
        канала, ни приём сообщения от него не рвутся. force=False — только если
        закрылось окно ожидания после старта."""
        if not hasattr(services, "update_regate"):       # роль без самообновления (заглушки)
            return
        try:
            if not force and not await asyncio.to_thread(services.update_hold_due):
                return
            nxt = await asyncio.to_thread(services.update_regate)
        except Exception as e:                            # noqa: BLE001
            log.warning("канал линка: пересчёт цели обновления: %s", e)
            return
        if nxt is not None and self.fn is not None:
            try:
                await self.fn(nxt)
            except Exception as e:                        # noqa: BLE001
                log.warning("канал линка: уведомление об обновлении не ушло: %s", e)
