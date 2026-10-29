"""Общая обвязка тестов записей и правки сообщений (lists/msgs): настоящий Bot на
сессии-заглушке эталонов с посредником (msgs.attach, как в бою), настоящая база
сервисов, замороженные часы хоста и подменённый сон правки (heal): длинные паузы (ожидание 429,
темп чата) тест не ждёт по-настоящему, но видит, сколько бы ждал.

Сессия умеет отказывать, как Telegram: правило «следующий такой-то запрос — такой
ответ» (400 «not found», 400 «can't be edited», 400 «not modified», 403, 429 с
retry_after, 5xx, 401, сетевой сбой)."""
from __future__ import annotations

import asyncio
import datetime as _dt
import json

from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import DeleteMessage, EditMessageReplyMarkup, EditMessageText, SendMessage

from awgbot.bot.lists import msgs
from awgbot.bot.lists.msgs import heal
from awgbot.core import config
from awgbot.infra.db import listsnap
from awgbot.util import timeutil
from tests.screens import harness

CHAT, OTHER = 5550001, 5550002
T0 = _dt.datetime(2026, 10, 27, 14, 5, 0, tzinfo=config.TZ)
HOUR = 3600
_REAL_SLEEP = asyncio.sleep

# ответы Telegram на отказ: (код, описание)
REFUSALS = {
    "not_found": (400, "Bad Request: message to edit not found"),
    "cant_edit": (400, "Bad Request: message can't be edited"),
    "not_modified": (400, "Bad Request: message is not modified: specified new message content and reply "
                          "markup are exactly the same as a current content and reply markup of the message"),
    "chat_not_found": (400, "Bad Request: chat not found"),
    "forbidden": (403, "Forbidden: bot was blocked by the user"),
    "flood": (429, "Too Many Requests: retry after {retry_after}"),
    "server": (502, "Bad Gateway"),
    "unauthorized": (401, "Unauthorized"),
}


class VTime:
    """Паузы heal без настоящего ожидания: сон от SKIP_FROM секунд не спит, а
    записывается (сколько просили ждать — `slept`); короткие паузы темпа —
    настоящие, чтобы по времени запросов было видно темп."""

    SKIP_FROM = 0.5

    def __init__(self):
        self.slept: list[float] = []

    async def sleep(self, s: float) -> None:
        s = float(s)
        self.slept.append(s)
        await _REAL_SLEEP(s if 0 < s < self.SKIP_FROM else 0)

    @property
    def t(self) -> float:
        return asyncio.get_running_loop().time()

    def long(self) -> list[float]:
        """Паузы, которые тест пропустил (ожидание 429, темп чата)."""
        return [s for s in self.slept if s >= self.SKIP_FROM]


class AsyncioProxy:
    """Модуль asyncio для heal (и уведомителя): sleep — через VTime, прочее
    (замки, задачи, gather, время цикла) — настоящее."""

    def __init__(self, vt: VTime):
        self._vt = vt

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, s, *a, **k):
        await self._vt.sleep(s)


class Session(harness.StubSession):
    """Заглушка эталонов с отказами по правилам и журналом запросов правки
    (виртуальное время, метод, чат, номер)."""

    def __init__(self, vt: VTime, now: _dt.datetime):
        super().__init__(CHAT, now, {})
        self.vt = vt
        self.rules: list[dict] = []
        self.edits: list[tuple[float, str, int, int]] = []

    def refuse(self, kind: str, *, method=(EditMessageText, EditMessageReplyMarkup), times: int = 1,
               retry_after: int = 5, chat: int | None = None, mid: int | None = None) -> None:
        self.rules.append({"kind": kind, "method": method, "times": times, "retry_after": retry_after,
                           "chat": chat, "mid": mid})

    def _rule(self, method):
        for r in self.rules:
            if r["times"] <= 0 or not isinstance(method, r["method"]):
                continue
            if r["chat"] is not None and int(getattr(method, "chat_id", 0) or 0) != r["chat"]:
                continue
            if r["mid"] is not None and int(getattr(method, "message_id", 0) or 0) != r["mid"]:
                continue
            r["times"] -= 1
            return r
        return None

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, (EditMessageText, EditMessageReplyMarkup)):
            self.edits.append((self.vt.t, type(method).__name__, int(method.chat_id), int(method.message_id)))
        rule = self._rule(method)
        if rule is not None:
            if rule["kind"] == "network":
                raise TelegramNetworkError(method=method, message="Request timeout error")
            code, desc = REFUSALS[rule["kind"]]
            body = {"ok": False, "error_code": code, "description": desc.format(retry_after=rule["retry_after"])}
            if code == 429:
                body["parameters"] = {"retry_after": rule["retry_after"]}
            return self.check_response(bot=bot, method=method, status_code=code,
                                       content=json.dumps(body)).result
        return await super().make_request(bot, method, timeout)


class Clock:
    """Часы хоста: timeutil.now() — t; шаг — advance(секунды)."""

    def __init__(self, monkeypatch, start: _dt.datetime = T0):
        self.t = start
        monkeypatch.setattr(timeutil, "now", lambda: self.t)

    def advance(self, seconds: float) -> None:
        self.t = self.t + _dt.timedelta(seconds=seconds)


class Rig:
    """Бот с посредником на заглушке, сервисы с настоящей базой, оба вида часов."""

    def __init__(self, services, monkeypatch, *, sent_at: _dt.datetime = T0):
        self.services = services
        self.db = services.db
        self.clock = Clock(monkeypatch)
        self.vt = VTime()
        monkeypatch.setattr(heal, "asyncio", AsyncioProxy(self.vt))
        heal._locks.clear()
        heal._last.clear()
        self.session = Session(self.vt, sent_at)
        self.bot = harness.make_bot(self.session, services)
        msgs.NOTE.set("")

    # ── снимки и тексты ──────────────────────────────────────────────────

    def snap(self, chat: int = CHAT, title: str = "⚠️ Не добавлено") -> str:
        return self.db.list_snap_keep(chat, "admin", title, [f"<code>r{i}.example</code> — не адрес" for i in range(8)])

    def fill(self, n: int, chat: int = CHAT) -> list[str]:
        return [self.snap(chat, f"№{i}") for i in range(n)]

    @staticmethod
    def link(key: str, label: str = "и ещё 5", new: bool = False) -> str:
        return f'<a href="https://t.me/{harness.BOT_USERNAME}?start=list-snap-{key}{"-n" if new else ""}">{label}</a>'

    def raw(self, key: str) -> dict | None:
        row = self.db._connection().execute("SELECT value FROM server_state WHERE key = ?",
                                            (listsnap.PREFIX + key,)).fetchone()
        return None if row is None else json.loads(row[0])

    def rec(self, mid: int, chat: int = CHAT) -> dict | None:
        return self.db.list_msg_get(chat, mid)

    def records(self) -> list[dict]:
        return self.db.list_msg_all()

    # ── что ушло в Telegram ──────────────────────────────────────────────

    def heads(self) -> list[str]:
        return [c.head for c in self.session.calls]

    def text_of(self, mid: int, chat: int = CHAT) -> str | None:
        return (self.session.messages.get((chat, mid)) or {}).get("text")

    def edit_calls(self, mid: int | None = None) -> list[harness.Call]:
        return [c for c in self.session.calls
                if c.head.startswith(("= edit #", "= markup #", "- markup off #"))
                and (mid is None or c.head.split("#", 1)[1].split()[0] == str(mid))]

    async def settle(self, cond, limit_s: float = 3.0) -> bool:
        """Дать фоновым задачам доработать (настоящее время, но коротко): cond() — готово."""
        loop = asyncio.get_running_loop()
        end = loop.time() + limit_s
        while loop.time() < end:
            if cond():
                return True
            await _REAL_SLEEP(0.01)
        return cond()


def rows(markup) -> list[list[tuple[str, str | None]]] | None:
    if markup is None:
        return None
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


__all__ = ["CHAT", "OTHER", "T0", "HOUR", "Rig", "Session", "VTime", "AsyncioProxy", "Clock", "rows", "REFUSALS",
           "SendMessage", "DeleteMessage", "EditMessageText", "EditMessageReplyMarkup"]
