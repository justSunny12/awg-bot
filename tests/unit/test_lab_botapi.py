"""Макет Bot API стенда (tests/lab/botapi.py): на нём сценарии резерва
проверяют, какой процесс отвечает в Telegram. Макет обязан вести себя как
настоящий Bot API в той части, на которую опирается резерв: опрос с
подтверждением через offset, отказ 409 при заданном вебхуке, вебхук с
заголовком секрета и повтором при отказе приёмника, журнал отправок.

Цена ошибки: макет, который отдаёт обновления при вебхуке, — сценарий
«основа не должна отвечать после переезда» зелёный, хотя на настоящем
Telegram она бы получила 409 (или наоборот); доставка без заголовка
секрета — приёмник бота основы на резерве, проверяющий секрет, на стенде
отвергает всё, и сценарии переезда падают не по делу; журнал, в котором
смешаны боты, — сценарий видит ответ не того бота.

Сервер — `aiohttp.test_utils.TestServer` на петле, приёмник вебхука — свой
aiohttp-приёмник там же, HTTP без TLS (сертификат не задан).
"""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tests.lab import botapi

pytestmark = pytest.mark.unit

TOKEN = "123456789:AA-DUMMY"
OTHER = "987654321:BB-DUMMY"


@pytest.fixture()
async def api(tmp_path):
    client = TestClient(TestServer(botapi.make_app(str(tmp_path / "journal.jsonl"))))
    await client.start_server()
    yield client
    await client.close()


async def _call(api, method, token=TOKEN, **params):
    r = await api.post(f"/bot{token}/{method}", json=params)
    return r.status, await r.json()


async def _inject(api, n, token=TOKEN):
    ids = []
    for i in range(n):
        r = await api.post(f"/inject/{token}", json={"message": {"message_id": i, "text": f"/start {i}"}})
        ids.append((await r.json())["update_id"])
    return ids


@pytest.fixture()
async def receiver():
    """Приёмник вебхука: копит (заголовок секрета, тело); `status` — что
    отвечать."""
    state = {"got": [], "status": 200}

    async def hook(request):
        state["got"].append((request.headers.get("X-Telegram-Bot-Api-Secret-Token"), await request.json()))
        return web.Response(status=state["status"])

    app = web.Application()
    app.router.add_post("/hook", hook)
    srv = TestServer(app)
    await srv.start_server()
    state["url"] = str(srv.make_url("/hook"))
    yield state
    await srv.close()


async def _until(cond, timeout=8.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return cond()


# ── опрос ────────────────────────────────────────────────────────────────────

async def test_get_updates_with_an_offset_confirms_what_came_before(api):
    ids = await _inject(api, 3)
    status, body = await _call(api, "getUpdates", offset=0, timeout=0)
    assert status == 200 and [u["update_id"] for u in body["result"]] == ids
    _, body = await _call(api, "getUpdates", offset=ids[1] + 1, timeout=0)
    assert [u["update_id"] for u in body["result"]] == ids[2:], "offset не подтвердил прежние"
    _, body = await _call(api, "getUpdates", offset=0, timeout=0)
    assert [u["update_id"] for u in body["result"]] == ids[2:], "подтверждённые вернулись"


async def test_long_polling_wakes_up_on_a_new_update(api):
    poll = asyncio.create_task(_call(api, "getUpdates", offset=0, timeout=5))
    await asyncio.sleep(0.2)
    ids = await _inject(api, 1)
    _, body = await asyncio.wait_for(poll, 3)
    assert [u["update_id"] for u in body["result"]] == ids


# ── вебхук ───────────────────────────────────────────────────────────────────

async def test_get_updates_is_refused_with_409_while_a_webhook_is_set(api, receiver):
    _, body = await _call(api, "setWebhook", url=receiver["url"], secret_token="DUMMY-secret")
    assert body == {"ok": True, "result": True}
    status, body = await _call(api, "getUpdates", offset=0, timeout=0)
    assert status == 409 and body["error_code"] == 409
    assert "can't use getUpdates method while webhook is active" in body["description"]
    await _call(api, "deleteWebhook")
    status, body = await _call(api, "getUpdates", offset=0, timeout=0)
    assert status == 200 and body["ok"], "после deleteWebhook опрос снова разрешён"


async def test_the_webhook_gets_the_update_with_the_secret_header(api, receiver):
    await _call(api, "setWebhook", url=receiver["url"], secret_token="DUMMY-secret")
    ids = await _inject(api, 2)
    assert await _until(lambda: len(receiver["got"]) == 2), "вебхук не получил обновления"
    assert [(s, u["update_id"]) for s, u in receiver["got"]] == [("DUMMY-secret", ids[0]), ("DUMMY-secret", ids[1])]
    _, body = await _call(api, "getWebhookInfo")
    info = body["result"]
    assert info["url"] == receiver["url"] and info["pending_update_count"] == 0
    assert info["has_custom_certificate"] is False and "last_error_message" not in info
    await _call(api, "deleteWebhook")


async def test_a_refusing_receiver_keeps_the_update_pending_and_shows_the_error(api, receiver):
    receiver["status"] = 500
    await _call(api, "setWebhook", url=receiver["url"], secret_token="DUMMY-secret")
    await _inject(api, 1)
    assert await _until(lambda: len(receiver["got"]) >= 1)

    async def info():
        return (await _call(api, "getWebhookInfo"))[1]["result"]
    got = await info()
    for _ in range(40):
        if "last_error_message" in got:
            break
        await asyncio.sleep(0.05)
        got = await info()
    assert got["pending_update_count"] == 1, "отвергнутое обновление потеряно"
    assert "500" in got.get("last_error_message", ""), got
    receiver["status"] = 200
    assert await _until(lambda: len(receiver["got"]) >= 2, timeout=6), "после отказа нет повтора"
    await _call(api, "deleteWebhook")


async def test_drop_pending_updates_clears_the_queue(api, receiver):
    await _inject(api, 2)
    await _call(api, "deleteWebhook", drop_pending_updates=True)
    _, body = await _call(api, "getUpdates", offset=0, timeout=0)
    assert body["result"] == []
    await _inject(api, 1)
    receiver["status"] = 500                                     # чтобы доставка не съела очередь сама
    await _call(api, "setWebhook", url=receiver["url"], drop_pending_updates=True)
    _, body = await _call(api, "getWebhookInfo")
    assert body["result"]["pending_update_count"] == 0
    await _call(api, "deleteWebhook")


# ── отправка и журнал ────────────────────────────────────────────────────────

async def test_send_message_answers_like_telegram_and_lands_in_the_journal(api):
    _, a = await _call(api, "sendMessage", chat_id=555001, text="первое")
    _, b = await _call(api, "sendMessage", chat_id=555001, text="второе")
    assert a["ok"] and b["result"]["message_id"] > a["result"]["message_id"]
    assert a["result"]["chat"]["id"] == 555001 and a["result"]["text"] == "первое"
    await _call(api, "sendMessage", token=OTHER, chat_id=1, text="чужой бот")
    r = await api.get(f"/journal/{TOKEN}")
    rows = await r.json()
    assert [(x["method"], x["params"]["text"]) for x in rows] == [("sendMessage", "первое"), ("sendMessage", "второе")], \
        "журнал бота смешан с чужим"


async def test_get_me_and_unknown_methods_answer_ok(api):
    _, me = await _call(api, "getMe")
    assert me["result"]["id"] == 123456789 and me["result"]["is_bot"] is True
    _, body = await _call(api, "setMyCommands", commands=[])
    assert body == {"ok": True, "result": True}
