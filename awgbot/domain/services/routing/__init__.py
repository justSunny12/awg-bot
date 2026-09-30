"""
routing/ — условная маршрутизация («🇷🇺 РФ-доступ»): доступ и списки, реконсиляция и источники, живость слотов, холодный старт, тексты.

routing.py — условная маршрутизация:
разрешения, устройства субъекта, домены, реконсиляция, источники списков,
живость шлюза.
"""

from .access import RoutingAccessMixin
from .reconcile import RoutingReconcileMixin
from .liveness import RoutingLivenessMixin
from .coldstart import RoutingColdStartMixin
from .notes import RoutingNotesMixin


class RoutingMixin(RoutingAccessMixin, RoutingReconcileMixin, RoutingLivenessMixin, RoutingColdStartMixin, RoutingNotesMixin):
    pass
