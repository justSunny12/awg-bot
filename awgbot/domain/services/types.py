"""
types.py — исключения, результаты операций и физические константы
сервисов. Здесь нет логики: миксины и внешний код импортируют это без
циклов.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# Байт в гигабайте — физическая константа (не настройка), потому в коде, не в
# config. Лимиты храним в байтах, вводим/показываем пользователю в ГБ.
# У texts/fmt.py есть свой приватный дубль (_BYTES_PER_GB) — намеренно, см. там.

BYTES_PER_GB = 1024 ** 3

# Секунд в сутках — физическая константа. Резервы/сроки храним в днях, но «долг»
# отсрочки и длительность паузы считаем в секундах (унифицировано с ISO-датами).
SECONDS_PER_DAY = 86400


# ─────────────────────────────────────────────────────────────────────────────
# Исключения и результаты
# ─────────────────────────────────────────────────────────────────────────────

class ServiceError(Exception):
    """Ошибка бизнес-операции (показывается пользователю)."""


class LimitReached(ServiceError):
    pass


@dataclass
class Notification:
    tg_id: int
    text: str
    reply_markup: object = None        # опциональная inline-клавиатура
    grace_offer_client_id: int = 0     # >0 → прикрепить кнопку отсрочки (делает scheduler)
    force_sound: bool = False          # True → слать со звуком даже в тихие часы
    critical: bool = False             # влияет на всех клиентов → при недоступном
                                       # Telegram уходит админу на почту (если включено)
    on_sent: object = None             # вызвать ПОСЛЕ доставки: смена состояния,
                                       # которая имеет смысл, только если адресат
                                       # уведомление получил (взведённый алерт)


@dataclass
class ClientCreated:
    client_id: int
    invite_code: str
    period_end: object            # datetime


@dataclass
class ActivationResult:
    ok: bool
    reason: str = ""              # invalid | already_has_access | ok
    client: Optional[object] = None
    upgrade: Optional["GuestUpgrade"] = None   # гость стал владельцем


@dataclass
class RoutingAddResult:
    """Разбор пачки доменов: что взято, что отброшено и почему.

    Отклонённые несут причину строкой — пользователь вставляет списком, и он
    должен видеть, что именно не прошло, а не гадать, почему добавилось меньше.
    """
    added: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    over_limit: int = 0            # не влезло в потолок списка
    limit: int = 0                 # сам потолок (для текста)


@dataclass
class DeviceCreated:
    device_id: int
    address: str
    vpn: str
    conf: str


@dataclass
class FriendActivation:
    """Итог активации кода F….
    reason: ok | invalid | own_device (код на устройство своего же профиля) |
    other_donor (у держателя уже есть устройства от другого владельца — в
    held/donor что и от кого)."""
    ok: bool
    reason: str = ""
    device_id: Optional[int] = None
    device_name: Optional[str] = None
    holder: Optional[object] = None      # профиль держателя (гость или клиент)
    donor: Optional[object] = None       # владелец устройства (при ok) / прежний даритель (other_donor)
    held: list = field(default_factory=list)   # устройства, которые держатель уже держит


@dataclass
class GuestUpgrade:
    """Гость стал владельцем: что перенесено и от кого."""
    donor: Optional[object] = None
    moved: list = field(default_factory=list)  # Device — перенесённые в новый профиль


@dataclass
class PauseCredit:
    """Что стало со счётом дней паузы при оплате периода kind.
    reason: credited — начислено (added < полного — упёрлись в порог) |
    cap — порог уже достигнут, не начислено | expired — ежемесячное после
    истечения | grace — в прошлом периоде была отсрочка | none — тип не копит."""
    kind: str
    before: int
    after: int
    cap: int
    reason: str

    @property
    def added(self) -> int:
        return self.after - self.before


@dataclass
class ExtendResult:
    new_end: object              # datetime
    notifications: list = field(default_factory=list)
    pause: Optional["PauseCredit"] = None


@dataclass
class DaysExtension:
    """Сдвиг конца подписки на N дней для одного адресата объявления с
    продлением. old_end/new_end — None у бессрочной: продлевать нечего,
    объявление уходит без шапки. from_now — истёкшая: отсчёт от сегодня, а не
    от старого конца."""
    client: object
    old_end: Optional[object] = None      # datetime
    new_end: Optional[object] = None      # datetime
    from_now: bool = False

    @property
    def unlimited(self) -> bool:
        return self.new_end is None
