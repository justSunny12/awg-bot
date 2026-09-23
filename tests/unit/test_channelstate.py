"""
Что домен знает о канале линка (domain/channelstate): открыта ли сессия и
сколько байт канал прогнал через линк по слоту.

Цена ошибки — ложная улика живости. Зонд по уликам вычитает байты канала из
счётчиков линка; вычти он лишнее — живой путь выглядит мёртвым и автомат
перекладывает трафик на резерв; не вычти — снимки, фиды и диагностика
сходят за обратный трафик клиентов, и мёртвый путь выглядит живым ровно
тогда, когда человек разбирается со сломанным слотом.
"""
from __future__ import annotations

import pytest

from awgbot.domain.channelstate import ChannelState

pytestmark = pytest.mark.unit


def test_minus_without_channel_traffic_returns_the_link_counters_as_they_are():
    """Канал через этот слот ничего не передавал — вычитать нечего. Минус
    накладных «на всякий случай» уводил бы счётчики в ноль на тихом линке."""
    ch = ChannelState()
    assert ch.minus(1, 5000, 7000) == (5000, 7000)
    assert ch.minus(0, 0, 0) == (0, 0)


def test_minus_takes_off_the_channel_bytes_and_the_overhead_per_message():
    """Каждое сообщение канала на счётчиках линка — его тело плюс заголовки и
    встречный ACK. Вычитается и то и другое, по числу сообщений."""
    ch = ChannelState()
    ch.account(1, rx=300)                         # снимок пришёл
    ch.account(1, tx=120)                         # роль ушла
    ch.account(1, rx=50, tx=40)
    o = ChannelState.OVERHEAD
    assert o == 200
    assert ch.minus(1, 10_000, 20_000) == (10_000 - 350 - 3 * o, 20_000 - 160 - 3 * o)


def test_the_slots_are_counted_apart():
    """Диагностика резервного слота не должна вычитаться из линка активного:
    иначе чужой канал съедал бы улики живого пути."""
    ch = ChannelState()
    ch.account(2, rx=1000, tx=1000)
    assert ch.minus(1, 5000, 5000) == (5000, 5000), "байты канала слота 2 вычтены из слота 1"
    assert ch.minus(2, 5000, 5000) == (5000 - 1000 - ch.OVERHEAD,) * 2
    ch.account("2", rx=1)                         # номер слота строкой — тот же слот
    assert ch.traffic[2].msgs == 2


def test_a_fresh_state_is_offline_and_empty():
    ch = ChannelState()
    assert ch.online is False, "до первой сессии признак «на связи» уже стоит"
    assert ch.traffic == {}


def test_both_roles_carry_their_own_channel_state():
    """Сервисы ВПС и агента заводят состояние канала сами: runtime пишет в него,
    домен читает. Общее на процесс (атрибут класса) смешало бы счёт двух
    экземпляров — в тестах и при пересоздании сервисов."""
    from awgbot.domain.gateway import GatewayServices
    from awgbot.domain.services import Services
    a, b, g = Services(None), Services(None), GatewayServices(None)   # БД здесь не нужна
    for svc in (a, b, g):
        assert isinstance(svc.channel, ChannelState)
        assert svc.channel.online is False
    a.channel.account(1, rx=10)
    assert b.channel.traffic == {} and g.channel.traffic == {}, "состояние канала общее на экземпляры"
