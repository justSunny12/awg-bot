"""
selfupdate/ — самообновление бота из GitHub-релизов, общее для обеих ролей.

Вынесено из Services без изменения поведения: агенту шлюза (роль gateway) нужен
ровно тот же механизм — выбрать цель, скачать, сверить sha256, запустить
апдейтер вне своего cgroup, отчитаться после рестарта, — и держать его в
клиентском классе значило бы тащить в агент две с половиной тысячи строк
чужой механики ради десяти методов. Примесь опирается только на self.db.

Части: core — цель, установка, уведомление, итог после рестарта, перезапуск;
gate — условие на версию другой роли (метаданные релиза: roles.gw.needs_main,
roles.main.needs_gw) для ступени набора и пропущенные релизы; cache —
сохранённый список релизов и кэш метаданных, выбор цели под одним замком;
marks — метки установки для `awg-bot.sh`: digest выпуска от установщика и
отметка жизни для сторожа обновления.

Цель обновления выбирает только основной бот — цель набора (domain/fleet);
агент шлюза обновляется лишь по команде сервера AWG (domain/gateway/upd.py) и
берёт отсюда установку, итог после перезапуска и метки.

SelfUpdateMixin — составной миксин частей под прежним именем.
"""
from __future__ import annotations

from .core import SelfUpdateCoreMixin, log, _notification
from .gate import SelfUpdateGateMixin, UpdatePeer, SERVER_OLD
from .cache import SelfUpdateCacheMixin


class SelfUpdateMixin(SelfUpdateCoreMixin, SelfUpdateGateMixin, SelfUpdateCacheMixin):
    """Самообновление. Подмешивается в Services и GatewayServices."""


__all__ = ["SelfUpdateMixin", "SelfUpdateCoreMixin", "SelfUpdateGateMixin", "SelfUpdateCacheMixin",
           "UpdatePeer", "SERVER_OLD", "log", "_notification"]
