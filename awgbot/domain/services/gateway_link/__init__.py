"""
gateway_link/ — линки до шлюзов: скрипт линка и файл конфигурации, слоты и их состояние, подсети за шлюзами, пометка устройства, бот агента.

gateway_link.py — шлюзы условной маршрутизации:
слоты, назначение и замена машины, бандл и токен агента по слоту,
переключение трафика, пинг со шлюза, предпочтительный слот, кто такой бот
шлюза слота (кэш getMe по отпечатку токена; спрашивает runtime/gwbotme.py).
"""

from .link import LinkScriptMixin
from .slots import SlotsMixin
from .peernets import PeerNetsMixin
from .mark import MarkMixin
from .agentbot import AgentBotMixin


class GatewayLinkMixin(LinkScriptMixin, SlotsMixin, PeerNetsMixin, MarkMixin, AgentBotMixin):
    pass
