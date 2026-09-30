"""
gateway/ — доменная механика роли gateway (docs/ROADMAP.md, п.7, этап 1). Части пакета:

  base      замок применения, запуск команд с таймаутом, дата-классы проверки и сводки
  link      линк, обвязка, ядро и версии — что агент видит на хосте
  monitor   гистерезис алертов, тик монитора, сводка, имя ВПС, потребление
  tgmark    путь к Telegram и GitHub: маркировка диапазонов, подсети соседей
  transit   VPN-транзит: обвязка локальной сети, свои домены, фиды по каналу
  report    что агент докладывает серверу каналом
  ownlists  свои списки, общие для всех шлюзов, и повтор после поломки
  peersvc   сервисы соседних сетей
  bundle    файл конфигурации из чата, настройки с сервера по каналу, резервная копия
  egress    выход наружу через локальный канал

Класс GatewayServices собирается здесь из миксинов частей и общих миксинов
domain/ (самообновление, резервные копии, почта, SSH).

Агент на шлюзе условной маршрутизации: наблюдаемость линка, обвязки и железа,
алерты с гистерезисом. НИКАКОЙ клиентской механики — у шлюза нет ни клиентов,
ни выдачи, ни awg-сервера; отдельный класс, а не наследник Services, потому что
из двух с половиной тысяч строк клиентского кода шлюзу не нужно ничего.

Команды — прямые (subprocess): роль живёт только на хосте, docker-плеча у неё
не бывает по построению. Каждая проба возвращает данные, а не бросает: панель
обязана рисоваться и на полумёртвом шлюзе — именно тогда она нужнее всего.
"""

from .base import (GwCheck, GwStatus, TIMEOUT_MARK, _APPLY_LOCK, _run, _out, _bundle_argv,
                   pathlib_read, log)
from . import base  # noqa: F401 — модуль хелперов: тесты подменяют base._run и base.pathlib_read
from .link import LinkMixin
from .monitor import MonitorMixin
from .tgmark import TgMarkMixin
from .transit import TransitMixin
from .report import ReportMixin
from .ownlists import OwnListsMixin
from .peersvc import PeerServicesMixin
from .bundle import BundleMixin
from .egress import EgressMixin
from awgbot.domain.selfupdate import SelfUpdateMixin
from awgbot.domain.backupcrypto import BackupCryptoMixin
from awgbot.domain.mailmix import MailMixin
from awgbot.domain.gwssh import GwSshMixin


class GatewayServices(SelfUpdateMixin, BackupCryptoMixin, MailMixin, GwSshMixin,
                      LinkMixin, MonitorMixin, TgMarkMixin, TransitMixin, ReportMixin,
                      OwnListsMixin, PeerServicesMixin, BundleMixin, EgressMixin):
    """Механика агента. db — обычная Database: нужен только state (гистерезис,
    снимки); клиентские таблицы просто пустуют, и городить отдельную схему ради
    их отсутствия — усложнение без выгоды."""

    GATEWAY_ROLE = True          # состав копии — шлюзовой и без оглядки на config.ROLE

    def __init__(self, db):
        self.db = db
        # канал до ВПС: открыта ли сессия и байты через линк — пишет
        # runtime/linkclient (слот 0), читают зонд наружу и задача списков
        from awgbot.domain.channelstate import ChannelState
        self.channel = ChannelState()


__all__ = ["GatewayServices", "GwCheck", "GwStatus", "TIMEOUT_MARK", "_APPLY_LOCK", "_run", "_out",
           "_bundle_argv", "pathlib_read", "log", "base"]
