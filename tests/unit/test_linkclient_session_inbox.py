"""
Клиент канала на шлюзе: очередь входящих (runtime/linkclient/dispatch.py) —
слова кворума мимо документа конфигурации, порядок прочего, замена не
начатого документа и дообработка прочитанного до конца сессии.

ВПС на том конце — `_Vps` с записанными строками; документ применяется в
потоке и висит на событии, пока тест его не отпустит.

Цена ошибки: слово кворума (qtake/qask/qneed), ждущее за документом,
который применяется минутами, — голос шлюза опаздывает к кандидату дольше
окна «не вижу», и кворум решает без него; прочие слова, обогнавшие документ, —
порядок, на который рассчитывает сервер (роль, фиды, ключи после
документа), сломан; документ, принятый к обработке и потерянный с концом
сессии, — сервер ждёт ответа, которого не будет; два устаревших документа
подряд — минуты лишнего применения уже заменённого; конец сессии, ждущий
документ и при отмене задачи, — остановка агента (перезапуск юнита) висит
минуты.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from awgbot.runtime import linkclient
from awgbot.util import gwlink
from tests.unit import _linkclient_session_common as _c
from tests.unit._linkclient_session_common import KEY, SN, _CfgAgent, _Conn, _live_client, _reply, _sent

pytestmark = pytest.mark.unit

conf, vps, gateway_role = _c.conf, _c.vps, _c.gateway_role


class _SlowAgent(_CfgAgent):
    """Документ применяется, пока тест не отпустит `release`; слова кворума и
    роли — в общий журнал `seen` в порядке обработки."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()
        self.applying = threading.Event()
        self.seen: list[str] = []

    def apply_cfg(self, msg: dict) -> dict:
        self.applying.set()
        self.seen.append(f"cfg:{msg.get('gen')}")
        assert self.release.wait(10), "тест не отпустил документ"
        return super().apply_cfg(msg)

    def quorum_guest_in(self, kind: str, body: dict) -> list:
        self.seen.append(kind)
        return []

    def set_link_role(self, active: bool, *a, **kw) -> None:
        self.seen.append("role")
        super().set_link_role(active, *a, **kw)


def _session(vps, *words: tuple[str, dict]) -> _Conn:
    """Сессия: роль первым словом, дальше `words`, потом ВПС молчит (сессия открыта)."""
    lines = [_reply(KEY, "role", {"active": True})]
    lines += [_reply(KEY, kind, body, seq=i + 2, first=False) for i, (kind, body) in enumerate(words)]
    conn = _Conn(lines, hang=True)
    vps.sessions.append(conn)
    return conn


async def _until(cond, seconds: float = 5.0) -> None:
    for _ in range(int(seconds / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("не дождались")


def _kinds(conn: _Conn) -> list[str]:
    return [gwlink.unpack(KEY, x, nonce=SN)["t"] for x in conn.written[1:]]


async def test_a_quorum_word_behind_a_slow_document_is_handled_at_once(vps):
    agent = _SlowAgent()
    conn = _session(vps, ("cfg", {"gen": 7}), ("qtake", {"l": "1", "e": 4}), ("qask", {"l": "1"}))
    client = linkclient.LinkClient(agent)
    task = asyncio.create_task(client._connect_once())
    try:
        await _until(lambda: "qask" in agent.seen)
        assert agent.docs == [], "документ уже применён — сценарий не тот"
        assert [w for w in agent.seen if w.startswith("q")] == ["qtake", "qask"], agent.seen
    finally:
        agent.release.set()
        await _until(lambda: agent.docs)
        conn.close()
        await task
    assert "cfg_ack" in _kinds(conn), "ответ на документ не ушёл по сессии"


async def test_other_words_keep_their_order_behind_the_document(vps):
    """Роль, пришедшая после документа, обрабатывается после него, как прежде."""
    agent = _SlowAgent()
    conn = _session(vps, ("cfg", {"gen": 7}), ("role", {"active": False}), ("qstate", {"l": "1", "e": 3}))
    client = linkclient.LinkClient(agent)
    task = asyncio.create_task(client._connect_once())
    try:
        await _until(lambda: "qstate" in agent.seen and agent.applying.is_set())
        assert agent.seen.count("role") == 1, f"роль обогнала документ: {agent.seen}"
    finally:
        agent.release.set()
        await _until(lambda: agent.seen.count("role") == 2)
        conn.close()
        await task
    assert [w for w in agent.seen if w != "qstate"] == ["role", "cfg:7", "role"], agent.seen


async def test_a_waiting_document_is_replaced_by_a_later_one(gateway_role, conf):
    """Первый в работе, второй и третий ждут: третий заменяет второй — применяются
    начатый и последний."""
    agent = _SlowAgent()
    client = _live_client(agent)
    await client._take({"t": "cfg", "gen": 7})
    try:
        await _until(agent.applying.is_set)
        await client._take({"t": "cfg", "gen": 8})
        await client._take({"t": "cfg", "gen": 9})
    finally:
        agent.release.set()
    await _until(lambda: len(agent.docs) == 2)
    await client._inbox_drain()
    assert [d["gen"] for d in agent.docs] == [7, 9], agent.docs
    assert [m["gen"] for m in _sent(client) if m["t"] == "cfg_ack"] == [7, 7], "ответов не по числу применений"


async def test_a_replacing_document_takes_the_place_of_the_replaced_one(gateway_role, conf):
    """Слова, пришедшие между вытесненным документом и его заменой, по-прежнему
    обрабатываются после документа: замена встаёт на место вытесненного, а не в
    конец очереди (иначе роль, зависящая от документа, обгоняла бы его)."""
    agent = _SlowAgent()
    client = _live_client(agent)
    await client._take({"t": "cfg", "gen": 7})
    try:
        await _until(agent.applying.is_set)
        await client._take({"t": "cfg", "gen": 8})
        await client._take({"t": "role", "active": False})
        await client._take({"t": "cfg", "gen": 9})
        assert [m.get("t") for m in client._inbox] == ["cfg", "role"], client._inbox
    finally:
        agent.release.set()
    await _until(lambda: len(agent.docs) == 2)
    await client._inbox_drain()
    assert agent.seen == ["cfg:7", "cfg:9", "role"], agent.seen


async def test_words_read_before_the_session_ends_are_handled_on_it(vps):
    """Документ и сразу конец сессии: документ применён, ответ ушёл по этой же
    сессии, и `_connect_once` вернулся только после этого."""
    agent = _CfgAgent()
    conn = _Conn([_reply(KEY, "role", {"active": True}), _reply(KEY, "cfg", {"gen": 7}, seq=2, first=False)])
    vps.sessions.append(conn)
    await linkclient.LinkClient(agent)._connect_once()
    assert [d["gen"] for d in agent.docs] == [7]
    assert "cfg_ack" in _kinds(conn), _kinds(conn)
    assert agent.pending == {}


async def test_cancelling_the_session_task_does_not_wait_for_a_slow_document(vps):
    """Задача сессии отменена (остановка клиента), пока документ применяется:
    конец сессии не ждёт его, обработчик снят, очередь брошена — иначе стоп
    агента (и перезапуск юнита) висел бы минуты за документом."""
    agent = _SlowAgent()
    conn = _session(vps, ("cfg", {"gen": 7}), ("role", {"active": False}))
    client = linkclient.LinkClient(agent)
    task = asyncio.create_task(client._connect_once())
    try:
        await _until(agent.applying.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert client._inbox_task is None or client._inbox_task.done(), "обработчик очереди пережил сессию"
        assert not client._inbox, "очередь входящих не брошена"
        assert agent.seen.count("role") == 1, "роль из брошенной очереди обработана после отмены"
    finally:
        agent.release.set()
        conn.close()


async def test_stop_during_the_end_of_session_drain_does_not_wait_either(vps, monkeypatch):
    """ВПС закрыл сессию, пока документ применяется: конец сессии дорабатывает
    очередь (ответ — по ней); остановка клиента в этот момент очередь бросает."""
    monkeypatch.setattr("awgbot.core.config.ROLE", "gateway")
    agent = _SlowAgent()
    conn = _Conn([_reply(KEY, "role", {"active": True}), _reply(KEY, "cfg", {"gen": 7}, seq=2, first=False)])
    vps.sessions.append(conn)
    client = linkclient.LinkClient(agent)
    client._task = asyncio.create_task(client._connect_once())
    try:
        await _until(agent.applying.is_set)
        await asyncio.sleep(0.05)
        assert not client._task.done(), "конец сессии не дождался документа"
        await asyncio.wait_for(client.stop(), timeout=2)
        assert client._task is None and client._inbox_task is None
    finally:
        agent.release.set()



async def test_a_session_cancel_in_the_same_turn_the_handler_finishes_is_not_swallowed(gateway_role, conf):
    """Обработчик доработал в тот же такт, когда пришла отмена сессии: дообработка
    отмену не глотает — задача сессии снята, а не идёт на новое подключение
    (stop() иначе ждал бы её без конца)."""
    client = _live_client(_CfgAgent())
    go = asyncio.Event()

    async def handler():
        await go.wait()

    client._inbox_task = asyncio.create_task(handler())
    session = asyncio.create_task(client._inbox_drain())
    await asyncio.sleep(0)                      # дообработка ждёт обработчик
    await asyncio.sleep(0)
    go.set()                                    # пробуждение обработчика — в очереди первым
    session.cancel()                            # отмена сессии — тем же тактом, следом
    await asyncio.wait({session}, timeout=2)
    assert client._inbox_task.done()
    assert session.cancelled(), "отмена сессии проглочена дообработкой"


async def test_a_handler_removed_alone_does_not_cancel_the_session(gateway_role, conf):
    """Снят только обработчик (сессию никто не отменял): дообработка возвращается
    без отмены — сессия кончается своим чередом."""
    client = _live_client(_CfgAgent())
    client._inbox_task = asyncio.create_task(asyncio.Event().wait())
    session = asyncio.create_task(client._inbox_drain())
    await asyncio.sleep(0)
    client._inbox_task.cancel()
    await asyncio.wait_for(session, timeout=2)
    assert not session.cancelled()

async def test_stop_does_not_wait_for_a_slow_document(vps, monkeypatch):
    monkeypatch.setattr("awgbot.core.config.ROLE", "gateway")
    agent = _SlowAgent()
    conn = _session(vps, ("cfg", {"gen": 7}))
    client = linkclient.LinkClient(agent)
    client._task = asyncio.create_task(client._connect_once())
    try:
        await _until(agent.applying.is_set)
        await asyncio.wait_for(client.stop(), timeout=2)
        assert client._task is None and client._inbox_task is None
    finally:
        agent.release.set()
        conn.close()
