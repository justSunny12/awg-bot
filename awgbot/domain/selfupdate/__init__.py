"""
selfupdate/ — самообновление бота из GitHub-релизов, общее для обеих ролей.

Вынесено из Services без изменения поведения: агенту шлюза (роль gateway) нужен
ровно тот же механизм — выбрать цель, скачать, сверить sha256, запустить
апдейтер вне своего cgroup, отчитаться после рестарта, — и держать его в
клиентском классе значило бы тащить в агент две с половиной тысячи строк
чужой механики ради десяти методов. Примесь опирается только на self.db.

Части: core — цель, установка, уведомление, итог после рестарта, перезапуск;
gate — условие на версию другой роли (метаданные релиза: roles.gw.needs_main,
roles.main.needs_gw): другая сторона, ожидание, молчащие шлюзы, пропущенные
релизы; cache — сохранённый список релизов и кэш метаданных, выбор цели под
одним замком и пересчёт без сети, когда изменились версии другой стороны;
hold — окно ожидания другой стороны после старта процесса; marks — метки
установки для `awg-bot.sh`: digest выпуска от установщика и отметка жизни для
сторожа обновления.

SelfUpdateMixin — составной миксин частей под прежним именем.
"""
from __future__ import annotations

from .core import SelfUpdateCoreMixin, log, _notification
from .gate import SelfUpdateGateMixin, UpdatePeer, UPDATE_PEER_FRESH_DAYS, SERVER_OLD
from .cache import SelfUpdateCacheMixin
from .hold import SelfUpdateHoldMixin, UPDATE_HOLD_MINUTES


class SelfUpdateMixin(SelfUpdateCoreMixin, SelfUpdateGateMixin, SelfUpdateCacheMixin,
                      SelfUpdateHoldMixin):
    """Самообновление. Подмешивается в Services и GatewayServices."""


__all__ = ["SelfUpdateMixin", "SelfUpdateCoreMixin", "SelfUpdateGateMixin", "SelfUpdateCacheMixin",
           "SelfUpdateHoldMixin", "UpdatePeer", "UPDATE_PEER_FRESH_DAYS", "UPDATE_HOLD_MINUTES",
           "SERVER_OLD", "log", "_notification"]
