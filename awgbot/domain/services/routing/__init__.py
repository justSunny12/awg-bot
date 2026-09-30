"""
routing/ — условная маршрутизация («🇷🇺 РФ-доступ»): разрешения, устройства
субъекта, домены, реконсиляция, источники списков, живость шлюза. Части пакета:

  common     журнал, тексты, константы — общее для частей
  access     обвязка, доступ профилям и устройствам, личный список доменов
  reconcile  реконсиляция таблиц и источники списков
  liveness   живость слотов: стрики, окно доступности, объявления
  coldstart  холодный старт: выбор активного слота при старте
  notes      тексты уведомлений маршрутизации

RoutingMixin — составной миксин частей под прежним именем.
"""

from .access import RoutingAccessMixin
from .reconcile import RoutingReconcileMixin
from .liveness import RoutingLivenessMixin
from .coldstart import RoutingColdStartMixin
from .notes import RoutingNotesMixin


class RoutingMixin(RoutingAccessMixin, RoutingReconcileMixin, RoutingLivenessMixin, RoutingColdStartMixin, RoutingNotesMixin):
    pass
