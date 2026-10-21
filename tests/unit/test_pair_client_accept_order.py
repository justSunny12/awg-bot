"""Канал пары, принимающая сторона (runtime/pairlink/client.py `_pairing_loop`):
на `pair_ok` сначала уходит `pair_admin` (на ключах приглашения), и только
после него пара записывается и линк переводится на новые ключи
(`reserve_accept_commit`); `pair_admin` не ушёл — пары нет
(`reserve_accept_abort("link")`), событие «refused» с причиной link.

Сервисы и `send_line` подменены записью вызовов в один журнал — порядок и
есть предмет проверки.

Цена ошибки: линк на новых ключах до ответа — пригласивший живёт на ключах
приглашения, `pair_admin` до него не доходит, у одной стороны пара, у другой
приглашение, и ни одна сессия больше не поднимается; пара, записанная при
неушедшем ответе, — та же половина пары.
"""
from __future__ import annotations

import pytest

from awgbot.runtime import pairlink
from awgbot.runtime.pairlink import client as client_mod

pytestmark = pytest.mark.unit


class _Svc:
    def __init__(self, log: list):
        self.log = log

    def reserve_accept_ok_in(self, msg):
        self.log.append("accept_ok_in")
        return {"sealed": "DUMMY-sealed"}

    def reserve_accept_commit(self):
        self.log.append("commit")

    def reserve_accept_abort(self, why):
        self.log.append(f"abort:{why}")

    def reserve_invite(self):
        return {"id": "DUMMY-invite"}

    def reserve_peer(self):
        return {"name": "Beta"}

    def reserve_linked_msg(self):
        return {}


@pytest.fixture()
def run(monkeypatch):
    log: list = []
    events: list = []

    async def on_event(name, st, extra):
        events.append((name, extra))
    monkeypatch.setattr(pairlink, "_on_event", on_event)

    async def go(sent_ok: bool):
        async def send_line(sess, kind, body=None):
            log.append(f"send:{kind}")
            return sent_ok
        monkeypatch.setattr(client_mod, "send_line", send_line)
        c = client_mod.PairClient(_Svc(log))
        await c._pairing_loop(None, object(), {"t": "pair_ok", "sealed": "DUMMY"}, 0)
        return c, log, events
    return go


async def test_pair_admin_goes_before_the_link_moves_to_the_new_keys(run):
    c, log, events = await run(True)
    assert log == ["accept_ok_in", "send:pair_admin", "commit"], \
        f"линк на новые ключи раньше ответа пригласившему: {log}"
    assert [e[0] for e in events] == ["linked"] and c._confirmed and not c._dead


async def test_pair_admin_that_did_not_go_writes_no_pair(run):
    c, log, events = await run(False)
    assert log == ["accept_ok_in", "send:pair_admin", "abort:link"], f"пара записана без ответа: {log}"
    assert events == [("refused", {"why": "link"})] and c._dead
