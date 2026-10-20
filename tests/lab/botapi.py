"""
botapi.py — макет Bot API Telegram для стенда: несколько ботов, очередь
обновлений с подтверждениями, опрос и вебхук, отказ 409, журнал отправок.

Боты стенда ходят сюда через `TelegramAPIServer.from_base(...)` (aiogram).
Правила — как у настоящего Bot API в той части, на которую опирается резерв:

- `getUpdates(offset, timeout)`: отдаёт обновления с id ≥ offset; всё, что
  меньше offset, считается подтверждённым и забывается. Пока у бота задан
  вебхук — 409 «Conflict: can't use getUpdates method while webhook is
  active; use deleteWebhook to delete the webhook first».
- `setWebhook(url, certificate, secret_token, …)` / `deleteWebhook` /
  `getWebhookInfo`: один адрес на бот; доставка — POST обновления на url с
  заголовком `X-Telegram-Bot-Api-Secret-Token`; сертификат из `setWebhook`
  — единственный доверенный; ответ не 2xx — повтор с нарастающим интервалом,
  в `getWebhookInfo` — `last_error_message`, `pending_update_count`.
- `sendMessage` и прочие методы отправки — строка в журнал (JSONL) и ответ,
  похожий на настоящий; неизвестные методы — `ok: true, result: true`.
- Служебное (не Bot API): `POST /inject/<token>` — положить обновление боту
  (тело — объект обновления без `update_id`); `GET /journal/<token>` — журнал.

Запуск: `python -m tests.lab.botapi --listen 127.0.0.1:8081 --journal /tmp/botapi.jsonl`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import tempfile
import time

from aiohttp import ClientSession, TCPConnector, web

CONFLICT = ("Conflict: can't use getUpdates method while webhook is active; "
            "use deleteWebhook to delete the webhook first")


class Bot:
    def __init__(self, token: str, journal: str) -> None:
        self.token = token
        self.journal = journal
        self.updates: list[dict] = []
        self.next_id = 1
        self.msg_id = 1
        self.webhook: dict | None = None
        self.last_error: tuple[int, str] | None = None
        self.wake = asyncio.Event()
        self.deliver_task: asyncio.Task | None = None

    @property
    def me(self) -> dict:
        uid = int(self.token.split(":", 1)[0]) if self.token.split(":", 1)[0].isdigit() else 1
        return {"id": uid, "is_bot": True, "first_name": f"bot{uid}", "username": f"lab{uid}_bot"}

    def log(self, method: str, params: dict) -> None:
        with open(self.journal, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "bot": self.me["id"], "method": method, "params": params},
                               ensure_ascii=False) + "\n")

    def inject(self, update: dict) -> int:
        uid = self.next_id
        self.next_id += 1
        self.updates.append({"update_id": uid, **update})
        self.wake.set()
        return uid

    def ack_below(self, offset: int) -> None:
        self.updates = [u for u in self.updates if u["update_id"] >= offset]

    async def poll(self, offset: int, timeout: float, limit: int) -> list[dict]:
        self.ack_below(offset)
        deadline = time.monotonic() + timeout
        while not self.updates and time.monotonic() < deadline:
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=max(0.0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
        return self.updates[:limit]

    # ── вебхук ──────────────────────────────────────────────────────────────
    def set_webhook(self, params: dict, cert_pem: bytes | None) -> None:
        self.webhook = {"url": params.get("url", ""), "secret": params.get("secret_token", ""),
                        "cert": cert_pem, "max_connections": int(params.get("max_connections", 40) or 40),
                        "set_at": time.time()}
        if str(params.get("drop_pending_updates", "")).lower() in ("true", "1"):
            self.updates.clear()
        self.last_error = None
        if self.deliver_task is None or self.deliver_task.done():
            self.deliver_task = asyncio.get_running_loop().create_task(self._deliver_loop())
        self.wake.set()

    def delete_webhook(self, drop_pending: bool) -> None:
        self.webhook = None
        if drop_pending:
            self.updates.clear()
        self.wake.set()

    def webhook_info(self) -> dict:
        info = {"url": self.webhook["url"] if self.webhook else "", "has_custom_certificate": bool(self.webhook and self.webhook["cert"]),
                "pending_update_count": len(self.updates)}
        if self.last_error:
            info["last_error_date"], info["last_error_message"] = self.last_error
        return info

    async def _deliver_loop(self) -> None:
        backoff = 1.0
        while self.webhook:
            if not self.updates:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                continue
            upd = self.updates[0]
            ok = await self._post(upd)
            if ok:
                self.ack_below(upd["update_id"] + 1)
                backoff = 1.0
            else:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _post(self, update: dict) -> bool:
        wh = self.webhook
        if not wh:
            return False
        ctx: ssl.SSLContext | bool = False
        if wh["cert"]:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
                f.write(wh["cert"])
                path = f.name
            ctx.load_verify_locations(path)
        headers = {"Content-Type": "application/json"}
        if wh["secret"]:
            headers["X-Telegram-Bot-Api-Secret-Token"] = wh["secret"]
        try:
            async with ClientSession(connector=TCPConnector(ssl=ctx)) as s:
                async with s.post(wh["url"], json=update, headers=headers, timeout=10) as r:
                    if 200 <= r.status < 300:
                        return True
                    self.last_error = (int(time.time()), f"Wrong response from the webhook: {r.status}")
        except Exception as e:  # noqa: BLE001
            self.last_error = (int(time.time()), f"Connection failed: {str(e)[:120] or e.__class__.__name__}")
        return False


class Api:
    def __init__(self, journal: str) -> None:
        self.journal = journal
        self.bots: dict[str, Bot] = {}

    def bot(self, token: str) -> Bot:
        if token not in self.bots:
            self.bots[token] = Bot(token, self.journal)
        return self.bots[token]

    async def handle(self, request: web.Request) -> web.Response:
        token, method = request.match_info["token"], request.match_info["method"]
        bot = self.bot(token)
        params: dict = {}
        cert: bytes | None = None
        if request.content_type.startswith("multipart/"):
            reader = await request.multipart()
            async for part in reader:
                if part.name == "certificate":
                    cert = await part.read()
                else:
                    params[part.name] = (await part.text())
        elif request.can_read_body:
            try:
                params = await request.json()
            except ValueError:
                params = dict(await request.post())
        params.update(request.query)
        return await self.call(bot, method, params, cert)

    async def call(self, bot: Bot, method: str, params: dict, cert: bytes | None) -> web.Response:
        def ok(result):
            return web.json_response({"ok": True, "result": result})
        if method == "getMe":
            return ok(bot.me)
        if method == "getUpdates":
            if bot.webhook:
                return web.json_response({"ok": False, "error_code": 409, "description": CONFLICT}, status=409)
            upd = await bot.poll(int(params.get("offset", 0) or 0), float(params.get("timeout", 0) or 0),
                                 int(params.get("limit", 100) or 100))
            return ok(upd)
        if method == "setWebhook":
            bot.set_webhook(params, cert)
            return ok(True)
        if method == "deleteWebhook":
            bot.delete_webhook(str(params.get("drop_pending_updates", "")).lower() in ("true", "1"))
            return ok(True)
        if method == "getWebhookInfo":
            return ok(bot.webhook_info())
        bot.log(method, params)
        if method in ("sendMessage", "editMessageText", "sendPhoto", "sendDocument", "sendAnimation"):
            bot.msg_id += 1
            chat = params.get("chat_id", 0)
            return ok({"message_id": bot.msg_id, "date": int(time.time()), "chat": {"id": int(chat) if str(chat).lstrip("-").isdigit() else 0, "type": "private"},
                       "text": params.get("text", "")})
        return ok(True)

    async def inject(self, request: web.Request) -> web.Response:
        bot = self.bot(request.match_info["token"])
        uid = bot.inject(await request.json())
        return web.json_response({"ok": True, "update_id": uid})

    async def journal_get(self, request: web.Request) -> web.Response:
        bot = self.bot(request.match_info["token"])
        try:
            with open(self.journal, encoding="utf-8") as f:
                rows = [json.loads(l) for l in f if l.strip()]
        except OSError:
            rows = []
        return web.json_response([r for r in rows if r.get("bot") == bot.me["id"]])


def make_app(journal: str) -> web.Application:
    api = Api(journal)
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_route("*", "/bot{token}/{method}", api.handle)
    app.router.add_post("/inject/{token}", api.inject)
    app.router.add_get("/journal/{token}", api.journal_get)
    return app


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--listen", default="127.0.0.1:8081")
    p.add_argument("--journal", default="/tmp/botapi.jsonl")
    a = p.parse_args()
    host, _, port = a.listen.rpartition(":")
    web.run_app(make_app(a.journal), host=host or "127.0.0.1", port=int(port))


if __name__ == "__main__":
    main()
