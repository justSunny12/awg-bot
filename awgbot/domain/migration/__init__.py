"""
migration.py — переезд профилей на второй интерфейс (docs/ROADMAP.md, п.3).

ЗАЧЕМ ОТДЕЛЬНЫЙ ИНТЕРФЕЙС. Всё, ради чего затевается перевыпуск — ротация
`Jc/S/H`, `RandomTrailers`, смена `ListenPort` и `dns1`, — это параметры уровня
ИНТЕРФЕЙСА. Два набора на одном интерфейсе не живут, поэтому переезд на месте
означал бы флаг-день: в момент переключения падают все, кто не успел. Причём
падают ровно те, до кого потом не достучаться — у кого Telegram работает через
этот же туннель.

Отсюда конструкция: рядом со старым интерфейсом поднимается новый, каждому
устройству рождается двойник, человек переимпортирует конфиг когда удобно, а
старый пир всё это время работает.

ЧТО ЗДЕСЬ ЕСТЬ И ЧЕГО НЕТ. Здесь доменная механика: состояние, когорта, рождение
двойников, оба выхода. Показ прогресса и уведомления — в слое бота; рассылку
объявления админ делает руками существующим механизмом.

Примесь к Services, а не свободные функции: механика насквозь опирается на
self.db и на уже написанные операции (_device_set_block, reconcile_ssh_access), а
services.py к этому моменту и без того на две с половиной тысячи строк.

Части: common — журнал, ключи состояния, итоги включения и прогресса, тип
отказа; state — состояние рычага и подготовка второго интерфейса; start —
когорта, включение рычага, рождение двойников; report — прогресс и обзор по
когорте; watch — поздравление пользователю, тик первых хендшейков, доклад о
готовности; exits — выходы: отмена, завершение, повышение интерфейса.

MigrationMixin — составной миксин частей под прежним именем.
"""
from __future__ import annotations

from .common import log, _STATE_KEY, STATE_RUNNING, STATE_OFF, LIVE_DAYS, MigrationStart, MigrationProgress, ServiceErrorMigration
from .state import MigrationStateMixin
from .start import MigrationStartMixin
from .report import MigrationReportMixin
from .watch import MigrationWatchMixin
from .exits import MigrationExitsMixin


class MigrationMixin(MigrationStateMixin, MigrationStartMixin, MigrationReportMixin,
                     MigrationWatchMixin, MigrationExitsMixin):
    """Механика переезда. Подмешивается в Services."""


__all__ = ["MigrationMixin", "MigrationStateMixin", "MigrationStartMixin",
           "MigrationReportMixin", "MigrationWatchMixin", "MigrationExitsMixin",
           "MigrationStart", "MigrationProgress", "ServiceErrorMigration",
           "log", "_STATE_KEY", "STATE_RUNNING", "STATE_OFF", "LIVE_DAYS"]
