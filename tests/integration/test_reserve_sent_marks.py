"""Отметки «отправлено» слов второй стороне по каналу пары
(domain/services/reserve/state.py PAIR_SENT_MEM, PAIR_SENT_VIEW): перечень
полон — каждая отметка `*_sent`/`*_sent_at` пакета резерва либо в нём, либо
названа здесь как отметка другого канала; новая сессия снимает все сессионные
одним вызовом; неушедшее слово (исполнитель действий, такт без канала)
снимает свою.

Цена ошибки: отметка, поставленная при сборке слова, оставалась после
неудачной отправки или обрыва сессии — `qkeys`, `qmem`, конфиг зонда,
блокировки, хвост журнала не уходили второй стороне до перезапуска или
следующей смены; кворум не включался, блокировки у резерва устаревали.
"""
from __future__ import annotations

import re
import types
from pathlib import Path

import pytest

from awgbot.domain.services.reserve.state import PAIR_SENT_MEM, PAIR_SENT_NOT_SESSION, PAIR_SENT_VIEW
from tests.integration import _reserve_common as rc

pytestmark = pytest.mark.integration
rsv = rc.rsv

ROOT = Path(__file__).resolve().parents[2]
RESERVE = ROOT / "awgbot" / "domain" / "services" / "reserve"
# отметки других каналов: гостевой канал шлюзов (state), локальный канал гостя
# (quorum, qcount, блокировки гостю) — у них свои сессии и свои сбросы
OTHER_CHANNELS = {"_rsv_state_sent", "_quo_guest_sent", "_quo_qcount_sent", "_guest_blocks_sent"}
_MEM = re.compile(r"""__dict__(?:\[|\.get\(|\.pop\()["'](_\w+_sent(?:_at)?)["']""")
_VIEW = re.compile(r"\b(\w+_sent)=")
_NOT_MARKS = {"go_sent", "up_sent", "eph_sent"}          # шаги автоматов переезда, а не отметки слов канала
ALL_MEM = [k for keys in PAIR_SENT_MEM.values() for k in keys]


def _found() -> tuple[set, set]:
    mem, view = set(), set()
    for f in RESERVE.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        mem |= set(_MEM.findall(text))
        view |= {m for m in _VIEW.findall(text) if m not in _NOT_MARKS}
    return mem, view


def test_every_sent_mark_of_the_reserve_package_is_listed():
    mem, view = _found()
    listed_view = {k for fields in PAIR_SENT_VIEW.values() for k in fields}
    assert mem - set(ALL_MEM) - OTHER_CHANNELS == set(), "отметка «отправлено» вне перечня сброса"
    assert view - listed_view == set(), "поле «отправлено» в rsv_view вне перечня сброса"
    assert set(ALL_MEM) <= mem, "в перечне отметка, которой в коде нет"


def _mark_all(services) -> None:
    for k in ALL_MEM:
        services.__dict__[k] = "был"
    services.rsv_update("rsv_view", qprobe_sent="abc", slots_sent=[1], slots_at="31.10.26")


def test_a_new_session_drops_every_session_mark(services, rsv):
    _mark_all(services)
    services.reserve_pair_sent_reset()
    left = {k for k in ALL_MEM if k in services.__dict__}
    assert left == {k for kind in PAIR_SENT_NOT_SESSION for k in PAIR_SENT_MEM[kind]}
    view = services.rsv_get("rsv_view")
    assert (view["qprobe_sent"], view["slots_sent"], view["slots_at"]) == ("", [], "")


@pytest.mark.parametrize("kind", sorted(set(PAIR_SENT_MEM) | set(PAIR_SENT_VIEW)))
def test_an_unsent_word_drops_only_its_mark(services, rsv, kind):
    _mark_all(services)
    services.reserve_pair_unsent(kind)
    gone = {k for k in ALL_MEM if k not in services.__dict__}
    assert gone == set(PAIR_SENT_MEM.get(kind, ()))
    view = services.rsv_get("rsv_view")
    assert (view["qprobe_sent"] == "") is (kind == "qprobe")
    assert (view["slots_sent"] == []) is (kind == "slots")


async def test_the_executor_drops_the_mark_of_a_word_that_did_not_go(monkeypatch):
    from awgbot.runtime import pairlink, rsvactions
    calls: list = []

    async def send(kind, body=None):
        return False
    monkeypatch.setattr(pairlink, "send", send)
    monkeypatch.setattr(rsvactions, "_ctx", {"services": types.SimpleNamespace(reserve_pair_unsent=calls.append)})
    await rsvactions.run([("send", "qkeys", {"l": "a"})])
    assert calls == ["qkeys"]
