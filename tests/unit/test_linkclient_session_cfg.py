"""
Клиент канала на шлюзе: документ конфигурации — ответ cfg_ack, его очередь
и ответ человека.
"""
from __future__ import annotations

import pytest

from awgbot.runtime import linkclient
from awgbot.util import gwlink
from tests.unit import _linkclient_session_common as _c
from tests.unit._linkclient_session_common import KEY, SN, _CfgAgent, _live_client, _sent

pytestmark = pytest.mark.unit

conf, vps, gateway_role, cfg_hooks = _c.conf, _c.vps, _c.gateway_role, _c.cfg_hooks


# ── документ конфигурации: ответ cfg_ack и его очередь ──────────────────────

async def test_a_document_is_applied_answered_and_the_queue_emptied(gateway_role, conf, cfg_hooks):
    """Документ с сервера: применён, ответ cfg_ack ушёл ровно один раз и
    очередь пуста — иначе он ушёл бы повторно в следующей сессии и сервер
    принял бы старый итог за новый."""
    agent = _CfgAgent()
    client = _live_client(agent)
    await client._apply_cfg({"t": "cfg", "gen": 7, "hash": "ab" * 8, "env": {}})
    assert len(agent.docs) == 1
    acks = [m for m in _sent(client) if m["t"] == "cfg_ack"]
    assert len(acks) == 1 and acks[0]["gen"] == 7 and acks[0]["applied"] == ["env"], acks
    assert "at" not in acks[0], "служебная отметка очереди ушла серверу"
    assert agent.pending == {}, "ответ ушёл, а в очереди остался"
    assert cfg_hooks == {"notes": [], "asks": []}, "человеку сказали, хотя сказать нечего"


async def test_the_note_goes_to_the_human_and_questions_to_the_menu(gateway_role, conf, cfg_hooks):
    """Что применено — одним уведомлением; почта и фраза, ждущие решения, —
    вопросами живого меню, по разделу на вызов, в порядке очереди."""
    agent = _CfgAgent({"ack": {"gen": 7, "hash": "ab" * 8, "applied": ["env"], "wait": {"mail": "cd" * 8},
                               "declined": {}, "failed": {}},
                       "note": "⚙️ Сервер AWG прислал конфигурацию шлюза — применено: резолвер",
                       "ask": ["mail", "backup"]})
    client = _live_client(agent)
    await client._apply_cfg({"t": "cfg", "gen": 7})
    assert cfg_hooks["notes"] == ["⚙️ Сервер AWG прислал конфигурацию шлюза — применено: резолвер"]
    assert cfg_hooks["asks"] == ["mail", "backup"]


async def test_a_broken_hook_does_not_lose_the_answer(gateway_role, conf, monkeypatch):
    """Чат недоступен (Telegram не ответил) — ответ серверу всё равно ушёл."""
    agent = _CfgAgent({**_CfgAgent().result, "note": "x", "ask": ["mail"]})

    async def boom(*a):
        raise RuntimeError("Telegram недоступен")
    monkeypatch.setattr(linkclient, "_notify", boom)
    monkeypatch.setattr(linkclient, "_ask", boom)
    client = _live_client(agent)
    await client._apply_cfg({"t": "cfg", "gen": 7})
    assert [m["t"] for m in _sent(client)].count("cfg_ack") == 1 and agent.pending == {}


async def test_an_answer_lost_with_the_session_waits_in_the_queue(gateway_role, conf, cfg_hooks):
    """Транзакция перезапустила линк — сессия, по которой пришёл документ,
    умерла: ответ остаётся в очереди, а не пропадает."""
    agent = _CfgAgent()
    client = _live_client(agent)

    def broken(data):
        raise ConnectionResetError("линк перезапущен")
    client._writer.write = broken
    await client._apply_cfg({"t": "cfg", "gen": 7})
    assert agent.pending.get("gen") == 7, "ответ, не ушедший с сессией, потерян"


async def test_the_queued_answer_goes_first_in_the_next_session(vps):
    """Новая сессия: ответ из очереди уходит первым сообщением, следом —
    снимок. Снимок раньше ответа показал бы серверу расхождение отпечатков
    без итога документа — и тот повторил бы доставку, не дождавшись причины."""
    agent = _CfgAgent()
    agent.pending = {"gen": 7, "hash": "ab" * 8, "applied": [], "wait": {}, "declined": {},
                     "failed": {"env": "линк не поднялся"}, "at": "2026-10-10T12:00:00+03:00"}
    conn = vps.answers()
    await linkclient.LinkClient(agent)._connect_once()
    kinds = [gwlink.unpack(KEY, x, nonce=SN)["t"] for x in conn.written[1:]]
    assert "cfg_ack" in kinds, f"ответ из очереди не ушёл в новой сессии: {kinds}"
    assert agent.pending == {}, "ответ ушёл, а очередь не пуста"
    assert kinds[0] == "cfg_ack", f"первым в новой сессии ушёл не ответ: {kinds}"


async def test_an_empty_queue_sends_nothing_in_a_new_session(vps):
    agent = _CfgAgent()
    conn = vps.answers()
    await linkclient.LinkClient(agent)._connect_once()
    kinds = [gwlink.unpack(KEY, x, nonce=SN)["t"] for x in conn.written[1:]]
    assert "cfg_ack" not in kinds, kinds


async def test_the_humans_answer_without_a_session_only_queues(monkeypatch):
    """Человек ответил на вопрос, а сессии нет: ответ — в очередь, уйдёт
    в следующей; ни исключения, ни попытки писать в несуществующий сокет."""
    monkeypatch.setattr(linkclient, "_client", None)
    agent = _CfgAgent()
    await linkclient.report_cfg_answer(agent, {"ok": True, "section": "mail", "accepted": True,
                                               "gen": 9, "hash": "ab" * 8, "print": "cd" * 8})
    assert agent.pending.get("applied") == ["mail"] and agent.pending.get("gen") == 9
    await linkclient.report_cfg_answer(agent, {"ok": False, "section": "mail"})
    assert agent.pending.get("gen") == 9, "пустой ответ переписал очередь"


async def test_the_humans_answer_with_a_session_goes_right_away(gateway_role, conf):
    agent = _CfgAgent()
    client = _live_client(agent)
    await linkclient.report_cfg_answer(agent, {"ok": True, "section": "backup", "accepted": True,
                                               "gen": 9, "hash": "ab" * 8, "print": "cd" * 8})
    acks = [m for m in _sent(client) if m["t"] == "cfg_ack"]
    assert len(acks) == 1 and acks[0]["applied"] == ["backup"] and agent.pending == {}
