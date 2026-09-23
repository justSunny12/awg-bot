"""
channelstate.py — что домен знает о канале линка, не заглядывая в runtime.

Клиент (на шлюзе) и слушатель (на ВПС) живут в `runtime/`; домену от них
нужны две вещи: открыта ли сессия и сколько байт канал прогнал через линк.
Раньше это лежало в `services.__dict__` под строковыми ключами — работало, но
границу между слоями было не видно. Теперь у сервисов есть `services.channel`,
runtime пишет в него, домен читает.

Байты считаются по слоту: на ВПС слотов несколько, на шлюзе один — там слот 0.
Зонд живости по уликам вычитает их из счётчиков линка, иначе снимки, фиды и
диагностика сходили бы за обратный трафик клиентов.
"""
from __future__ import annotations


class ChannelTraffic:
    """Байты и число сообщений канала через линк одного слота."""
    __slots__ = ("rx", "tx", "msgs")

    def __init__(self) -> None:
        self.rx = 0
        self.tx = 0
        self.msgs = 0

    def add(self, rx: int = 0, tx: int = 0) -> None:
        self.rx += rx
        self.tx += tx
        self.msgs += 1


class ChannelState:
    # накладные на сообщение поверх тела: заголовки TCP/IP, конверт AWG,
    # встречный ACK — с запасом
    OVERHEAD = 200

    def __init__(self) -> None:
        self.online = False                      # шлюз: сессия с ВПС открыта
        self.traffic: dict[int, ChannelTraffic] = {}

    def account(self, slot_id: int, rx: int = 0, tx: int = 0) -> None:
        self.traffic.setdefault(int(slot_id), ChannelTraffic()).add(rx, tx)

    def minus(self, slot_id: int, link_rx: int, link_tx: int) -> tuple[int, int]:
        """Счётчики линка за вычетом канала этого слота."""
        t = self.traffic.get(int(slot_id))
        if t is None:
            return link_rx, link_tx
        extra = t.msgs * self.OVERHEAD
        return link_rx - (t.rx + extra), link_tx - (t.tx + extra)


__all__ = ["ChannelState", "ChannelTraffic"]
