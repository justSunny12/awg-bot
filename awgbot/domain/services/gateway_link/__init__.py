"""
gateway_link/ — шлюзы условной маршрутизации: слоты, назначение и замена
устройства, файл конфигурации и токен агента по слоту, переключение трафика,
пинг со шлюза, предпочтительный слот, кто такой бот шлюза слота (кэш getMe по
отпечатку токена; спрашивает runtime/gwbotme.py). Части пакета:

  common    журнал, тексты, константы — общее для частей
  link      скрипт линка и сборка файла конфигурации шлюза
  slots     слоты шлюзов, переключение трафика, пинг, состояние для экранов
  drift     расхождение конфигурации по снимку канала: уведомления
  peernets  VPN-транзит слота и связь подсетей (его часть): префиксы слотов в туннеле
  mark      пометка устройства шлюзом: запасной путь через пересланный токен
  agentbot  токен и имя бота агента по слотам
  rekey     смена ключей линка тем же устройством каналом: стадия, переход, сроки
  linki1    I1 линка — строка I1 интерфейса AWG сервера как есть; что стоит у линка
  rekey_i1  I1 живым линкам: автоматическая смена ключей по одному слоту, повторы, строка карточки
  token     аварийная смена токена бота шлюза: кому можно, проверка «тот же бот»
  lanmap    адреса в туннеле для локальных сетей шлюзов: почему выключены, записи
            таблицы awg_lan_map, пары для экранов, перевод записей SMB
  lanmap_sync  сверка таблицы и политики слотов, старт, уведомление
  slotkeys  реестр слотовых ключей server_state: что снимается со слотом,
            что — при замене устройства (не миксин, общий для gwchannel/)

GatewayLinkMixin — составной миксин частей под прежним именем.
"""

from .link import LinkScriptMixin
from .slots import SlotsMixin
from .drift import DriftMixin
from .peernets import PeerNetsMixin
from .mark import MarkMixin
from .agentbot import AgentBotMixin
from .rekey import RekeyMixin
from .linki1 import LinkI1Mixin
from .rekey_i1 import RekeyI1Mixin
from .token import TokenMixin
from .lanmap import LanMapMixin
from .lanmap_sync import LanMapSyncMixin


class GatewayLinkMixin(LinkScriptMixin, SlotsMixin, DriftMixin, PeerNetsMixin, MarkMixin, AgentBotMixin, RekeyMixin,
                       LinkI1Mixin, RekeyI1Mixin, TokenMixin, LanMapMixin, LanMapSyncMixin):
    pass
