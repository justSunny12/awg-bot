"""
gateway_link/ — шлюзы условной маршрутизации: слоты, назначение и замена
устройства, файл конфигурации и токен агента по слоту, переключение трафика,
пинг со шлюза, предпочтительный слот, кто такой бот шлюза слота (кэш getMe по
отпечатку токена; спрашивает runtime/gwbotme.py). Части пакета:

  common    журнал, тексты, константы — общее для частей
  link      скрипт линка и сборка файла конфигурации шлюза
  slots     слоты шлюзов, переключение трафика, пинг, состояние для экранов
  peernets  VPN-транзит слота и связь подсетей (его часть): префиксы слотов в туннеле
  mark      пометка устройства шлюзом: запасной путь через пересланный токен
  agentbot  токен и имя бота агента по слотам
  rekey     смена ключей линка тем же устройством каналом: стадия, переход, сроки
  token     аварийная смена токена бота шлюза: кому можно, проверка «тот же бот»
  lanmap    адреса в туннеле для локальных сетей шлюзов: таблица awg_lan_map, политика
            слотов, перевод записей SMB, проверка пространства, уборка 3.2.x

GatewayLinkMixin — составной миксин частей под прежним именем.
"""

from .link import LinkScriptMixin
from .slots import SlotsMixin
from .peernets import PeerNetsMixin
from .mark import MarkMixin
from .agentbot import AgentBotMixin
from .rekey import RekeyMixin
from .token import TokenMixin
from .lanmap import LanMapMixin


class GatewayLinkMixin(LinkScriptMixin, SlotsMixin, PeerNetsMixin, MarkMixin, AgentBotMixin, RekeyMixin,
                       TokenMixin, LanMapMixin):
    pass
