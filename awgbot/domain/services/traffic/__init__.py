"""
traffic/ — опрос трафика, лимиты потребления, сроки и истечение, месячные сбросы.

Части: texts — тексты уведомлений; poll — опрос и учёт РФ-трафика; limits —
лимиты потребления и бонус при исчерпании; bonus — правило бонуса (разбор
ввода «50» / «10%» / «10% 50» / «0», расчёт от лимита профиля, подписи);
expiry — сроки подписок; resets — месячные сбросы.
"""
from .texts import _TXT_EXPIRED_CLIENT, _reset_client_text, _reset_friend_text
from .poll import PollMixin
from .limits import LimitsMixin
from .expiry import ExpiryMixin
from .resets import ResetsMixin


class TrafficMixin(PollMixin, LimitsMixin, ExpiryMixin, ResetsMixin):
    """Трафик, лимиты, сроки — составной миксин частей под прежним именем."""


__all__ = ["TrafficMixin", "PollMixin", "LimitsMixin", "ExpiryMixin", "ResetsMixin",
           "_TXT_EXPIRED_CLIENT", "_reset_client_text", "_reset_friend_text"]
