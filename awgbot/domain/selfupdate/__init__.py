"""
selfupdate/ — самообновление бота из GitHub-релизов, общее для обеих ролей.

Вынесено из Services без изменения поведения: агенту шлюза (роль gateway) нужен
ровно тот же механизм — выбрать цель, скачать, сверить sha256, запустить
апдейтер вне своего cgroup, отчитаться после рестарта, — и держать его в
клиентском классе значило бы тащить в агент две с половиной тысячи строк
чужой механики ради десяти методов. Примесь опирается только на self.db.

Части: core — цель, установка, уведомление, итог после рестарта, перезапуск.

SelfUpdateMixin — составной миксин частей под прежним именем.
"""
from __future__ import annotations

from .core import SelfUpdateCoreMixin, log, _notification


class SelfUpdateMixin(SelfUpdateCoreMixin):
    """Самообновление. Подмешивается в Services и GatewayServices."""


__all__ = ["SelfUpdateMixin", "SelfUpdateCoreMixin", "log", "_notification"]
