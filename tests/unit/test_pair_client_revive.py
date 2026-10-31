"""Канал пары, принимающая сторона (runtime/pairlink `ensure`): клиент, чей
цикл вышел насовсем после `pair_refused` или срыва `pair_admin` (`dead`),
при новом принятии заменяется новым — `ensure` с ролью «принявший» не
довольствуется живым объектом с мёртвым циклом.

Цена ошибки: админ отменил приглашение (или вторая сторона отказала) и в ту же
минуту принял новое — клиент прежнего сопряжения не подключается, код не
приходит, пока бот не перезапущен; админ видит вечное «ждём ответа».
"""
from __future__ import annotations

import asyncio

import pytest

from awgbot.runtime import pairlink
from awgbot.runtime.pairlink.client import PairClient

pytestmark = pytest.mark.unit


class _Svc:
    def __init__(self):
        self.connects = 0

    def reserve_link_role(self):
        return "acceptor"

    def reserve_connect_address(self):
        self.connects += 1
        return None                                  # дальше подключения не идём

    def reserve_channel_secret(self):
        return ""

    def reserve_invite(self):
        return {}


async def test_dead_client_is_replaced_on_new_accept(monkeypatch):
    svc = _Svc()
    old = PairClient(svc)
    old._dead = True                                 # прежнее сопряжение: pair_refused
    monkeypatch.setattr(pairlink, "_link", old)
    try:
        await pairlink.ensure(svc)                   # новое принятие (accept.py)
        pairlink.wake()
        await asyncio.sleep(0.2)
        assert pairlink._link is not old and not pairlink._link.dead, "мёртвый клиент остался на месте"
        assert svc.connects >= 1, "новое принятие не подключается"
    finally:
        await pairlink.shutdown()


async def test_live_client_is_kept(monkeypatch):
    svc = _Svc()
    live = PairClient(svc)
    monkeypatch.setattr(pairlink, "_link", live)
    try:
        await pairlink.ensure(svc)
        assert pairlink._link is live, "живой клиент пересоздан на каждом такте"
    finally:
        await pairlink.shutdown()
