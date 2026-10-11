"""
gwchannel/ — сторона ВПС: приём того, что привёз канал линка (части: session, snapshot,
own, bundle, services, settings, feeds, card, claim, cfg — по смыслу сообщений;
smbtunnel — «SMB в туннеле»: записи резолвера админа из SMB всех слотов).

Здесь хранение, сверка и решение, что доставить шлюзу. Снимок рисуется на
экранах и сравнивается с тем, что ВПС сам же выдал в конфигурации; расхождение
определяет, какие настройки уйдут каналом (этап 2). Фиды локальной сети для
шлюзов ВПС качает здесь же (этап 3); здесь же канон своих списков, общих для
всех шлюзов, — слияние правок шлюзов и строка карточки слота. В автомат переключения слотов снимок не
входит: данные приехали с чужой машины, и доверять им выбор пути было бы
странно.

Два времени, а не одно, — намеренно. Возраст снимка считается от времени
ПРИЁМА по часам ВПС; `ts` шлюза хранится справочно. Машина без RTC, поднявшаяся
раньше сети, нарисовала бы «состояние два часа назад» у совершенно свежего
снимка или, при ошибке в другую сторону, «минус три минуты».
"""
from .common import drift_lines      # noqa: F401
from .session import SessionMixin
from .snapshot import SnapshotMixin
from .own import OwnListsMixin
from .bundle import BundleMixin
from .services import ServicesMixin
from .settings import SettingsMixin
from .feeds import FeedsMixin
from .card import CardMixin
from .claim import ClaimMixin
from .cfg import CfgMixin
from .smbtunnel import SmbTunnelMixin


class GwChannelMixin(SessionMixin, SnapshotMixin, OwnListsMixin, BundleMixin, ServicesMixin,
                     SettingsMixin, FeedsMixin, CardMixin, ClaimMixin, CfgMixin, SmbTunnelMixin):
    """Сторона ВПС канала линка — собирается из частей пакета (см. шапку)."""


__all__ = ["GwChannelMixin", "drift_lines"]
