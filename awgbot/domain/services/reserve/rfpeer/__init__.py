"""
rfpeer — РФ-доступ через второй сервер (пакет reserve, см. его __init__):
когда свои шлюзы не отвечают, РФ-трафик пользователей идёт через линк пары
на второй сервер и его шлюзы; пользователи, бот, эпоха, ключи и голоса не
трогаются. Симметрично: любой сервер пары может быть транзитным для другого.

- `offer` — сторона транзитного: согласие полем `st.rfp` (тумблер «Мои шлюзы
  для резерва», свой живой слот, сам не в транзите), файл согласия для
  файервола (`reserve/rfpeer.json`), правило маршрутизации с линка пары,
  эпизод второй стороны (Т-РТ10/Т-РТ11 со счётчиком);
- `slot` — сторона просящего: виртуальный слот «шлюзы второго» (id 29, линк
  `awgpair`) в обычном автомате слотов — последний кандидат, не липкий;
  `AllowedIPs` линка пары рантаймом, уход с пропавшего слота, факт
  пользования в `st.rfp.use`, Т-РТ6/Т-РТ7/Т-РТ8;
- `screen` — строки карточки обеих сторон и экрана роли.
"""
from .offer import ReserveRfOfferMixin
from .screen import ReserveRfScreenMixin
from .slot import RF_PEER_ID, RF_PEER_IF, ReserveRfSlotMixin


class ReserveRfPeerMixin(ReserveRfOfferMixin, ReserveRfSlotMixin, ReserveRfScreenMixin):
    pass


__all__ = ["ReserveRfPeerMixin", "RF_PEER_ID", "RF_PEER_IF"]
