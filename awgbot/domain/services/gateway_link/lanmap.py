"""lanmap.py — адреса в туннеле для локальных сетей шлюзов: проверка служебного
пространства по конфигурации сервера (схема и формулы — util/lanspace)."""

from __future__ import annotations

from awgbot.core import config
from awgbot.util import lanspace


class LanMapMixin:
    """Адреса в туннеле для локальных сетей шлюзов."""

    def lan_space_problem(self) -> str:
        """Причина, по которой адреса в туннеле включать нельзя, или "": служебное
        пространство задевает подсети клиентов (с подсетью переезда) или /30
        линков, либо резолвер админа или адрес перехвата DNS стоят внутри него
        вне служебной /16. Линки — и из настроек слотов, и заведённые в БД."""
        from awgbot.infra import admindns, routing
        clients = [net for net, _iface in config.routing_client_subnets()]
        links = list(config.ROUTING_LINK_CIDRS)
        for gw in self.db.gateways():
            if gw.link_cidr and gw.link_cidr not in links:
                links.append(gw.link_cidr)
        return lanspace.space_problem(lanspace.space(), clients, links,
                                      admindns.addr(), routing.DNS_INTERCEPT_ADDR)
