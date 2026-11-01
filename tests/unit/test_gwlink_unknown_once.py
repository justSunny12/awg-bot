"""
Канал шлюзов — незнакомое слово: строка «неизвестное — игнорирую» раз на вид
за сессию (`pairproto.first_unknown`, как в канале пары и гостевом), у
слушателя ВПС (runtime/linkserver/inbound — множество в `_Session`) и у
клиента шлюза (runtime/linkclient/dispatch — множество сессии клиента,
сбрасывается на подключении).

Цена ошибки: слово будущего выпуска, которое другая сторона шлёт на каждой
доставке или такте, — строка в журнал на каждое сообщение: журнал шлюза
(малый диск) и ВПС забиваются одной и той же строкой, а настоящие записи
тонут; молчание вовсе — незнакомое слово не видно при разборе
несовместимости.
"""
from __future__ import annotations

import logging
import types

import pytest

from awgbot.runtime import linkclient, linkserver
from awgbot.runtime.linkserver.common import _Session
from tests.unit import _linkclient_session_common as _c
from tests.unit._linkclient_session_common import KEY, _Agent, _Conn, _reply

pytestmark = pytest.mark.unit

conf, vps = _c.conf, _c.vps


def _lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "неизвестное" in r.getMessage()]


def _fresh() -> _Session:
    s = _Session(writer=None, key=b"k" * 32)
    s.hello = True
    return s


async def _feed(server, sess) -> None:
    gw = types.SimpleNamespace(id=3)
    for kind in ("zz_future", "zz_future", "zz_other", "zz_future"):
        await server._handle(gw, sess, {"t": kind})


async def test_the_server_logs_an_unknown_word_once_per_kind_per_session(caplog):
    caplog.set_level(logging.INFO)
    server = linkserver.LinkServer(types.SimpleNamespace())
    await _feed(server, _fresh())
    got = _lines(caplog)
    assert len(got) == 2, got
    assert "'zz_future'" in got[0] and "'zz_other'" in got[1]
    await _feed(server, _fresh())
    assert len(_lines(caplog)) == 4, "новая сессия слота — строка о том же виде снова"


def _session_with_unknown_words(vps) -> None:
    words = [_reply(KEY, "role", {"active": True})]
    words += [_reply(KEY, kind, {}, seq=n, first=False)
              for n, kind in enumerate(("zz_future", "zz_future", "zz_other", "zz_future"), start=2)]
    vps.sessions.append(_Conn(words))


async def test_the_client_logs_an_unknown_word_once_per_kind_per_session(vps, caplog):
    caplog.set_level(logging.INFO)
    client = linkclient.LinkClient(_Agent())
    _session_with_unknown_words(vps)
    _session_with_unknown_words(vps)
    await client._connect_once()
    got = _lines(caplog)
    assert len(got) == 2, got
    assert "«zz_future»" in got[0] and "«zz_other»" in got[1]
    await client._connect_once()
    assert len(_lines(caplog)) == 4, "новая сессия с ВПС — строка о том же виде снова"
