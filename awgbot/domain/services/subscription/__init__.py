"""
subscription/ — подписка: продление, правка дат, +N дней объявлением,
отсрочка, приостановка и счёт дней паузы, чистка истории.

Части: period — границы периода (продление, правка дат, +N дней, отсрочка
годовой); credit — счёт дней паузы (начисление и пороги — их читают и
продление, и создание профиля); pause — вход и выход из приостановки,
email-выход, авто-выход по сроку; history — чистка истории по ретеншну.
"""
from .period import PeriodMixin
from .credit import CreditMixin
from .pause import PauseMixin
from .history import HistoryMixin


class SubscriptionMixin(PeriodMixin, CreditMixin, PauseMixin, HistoryMixin):
    """Подписка — составной миксин частей под прежним именем."""


__all__ = ["SubscriptionMixin", "PeriodMixin", "CreditMixin", "PauseMixin", "HistoryMixin"]
