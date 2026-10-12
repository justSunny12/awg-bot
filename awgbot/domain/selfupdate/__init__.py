"""
selfupdate/ — самообновление бота из GitHub-релизов, общее для обеих ролей.

Вынесено из Services без изменения поведения: агенту шлюза (роль gateway) нужен
ровно тот же механизм — выбрать цель, скачать, сверить sha256, запустить
апдейтер вне своего cgroup, отчитаться после рестарта, — и держать его в
клиентском классе значило бы тащить в агент две с половиной тысячи строк
чужой механики ради десяти методов. Примесь опирается только на self.db.

Части: core — цель, установка, уведомление, итог после рестарта, перезапуск;
gate — условие на версию другой роли (#gw_needs_main_X / #main_needs_gw_X):
другая сторона, ожидание, молчащие шлюзы; cache — сохранённый список релизов и
пересчёт цели по нему без сети, когда изменились версии другой стороны.

SelfUpdateMixin — составной миксин частей под прежним именем.
"""
from __future__ import annotations

from .core import SelfUpdateCoreMixin, log, _notification
from .gate import SelfUpdateGateMixin, UpdatePeer, UPDATE_PEER_FRESH_DAYS, SERVER_OLD
from .cache import SelfUpdateCacheMixin


class SelfUpdateMixin(SelfUpdateCoreMixin, SelfUpdateGateMixin, SelfUpdateCacheMixin):
    """Самообновление. Подмешивается в Services и GatewayServices."""


__all__ = ["SelfUpdateMixin", "SelfUpdateCoreMixin", "SelfUpdateGateMixin", "SelfUpdateCacheMixin",
           "UpdatePeer", "UPDATE_PEER_FRESH_DAYS", "SERVER_OLD", "log", "_notification"]
