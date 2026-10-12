"""reconcile/ — реконсиляция таблиц и источники списков.

Части: tables — реконсиляция наборов, цепочки и конфига dnsmasq, кэш скачанных
списков, доклад о падении реконсиляции; sources — источники списков: учёт
исходов, доклады, обновление и сводка; probes — зонды слотов: активного,
резерва, всех разом.

RoutingReconcileMixin — составной миксин частей под прежним именем.
"""

from .tables import RoutingTablesMixin
from .sources import RoutingSourcesMixin
from .probes import RoutingProbesMixin


class RoutingReconcileMixin(RoutingTablesMixin, RoutingSourcesMixin, RoutingProbesMixin):
    """Реконсиляция таблиц и источники списков."""


__all__ = ["RoutingReconcileMixin", "RoutingTablesMixin", "RoutingSourcesMixin",
           "RoutingProbesMixin"]
