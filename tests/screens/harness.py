"""Обвязка эталонов экранов: настоящий диспетчер роли на заглушке сессии Bot API.

Снимок гонится так же, как живое нажатие: событие (колбэк, /start, текст)
уходит в `make_dispatcher(role, …)` — AccessMiddleware, фильтры роутеров,
разбор упакованного колбэка, — а всё, что бот после этого сказал Telegram,
ловит заглушка сессии и пишет по порядку. Запись сериализуется в текст
эталона; рядом — проверки ограничений Telegram и разметки HTML, которые
гоняются по каждому снимку.

Формат записи:

    ### <id> — <заголовок>
    > press <упакованный колбэк>  |  > /start <payload>  |  > text «…»
    ~ всплывашка  (~! — alert;  ~ (пустой ответ))
    = edit #N | + send #N | + document #N | - delete #N | - markup off #N
    <текст HTML как ушёл>
      [подпись | callback_data] [подпись | url] …

Нумерация сообщений — своя в каждом снимке: сообщение, на котором нажата
кнопка (или которое прислал человек), — #1, новые сообщения бота — дальше.
Подготовительные действия снимка выполняются, но не печатаются — только
последнее.
"""
from __future__ import annotations

import datetime as _dt
import html as _html
import itertools
import json
import re
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.filters.callback_data import CallbackData
from aiogram.methods import (AnswerCallbackQuery, DeleteMessage, DeleteMessages, EditMessageCaption,
                             EditMessageMedia, EditMessageReplyMarkup, EditMessageText, GetMe,
                             SendAnimation, SendDocument, SendMediaGroup, SendMessage, SendPhoto,
                             SetMyCommands, DeleteMyCommands, SetChatMenuButton)
from aiogram.types import (CallbackQuery, Chat, InlineKeyboardMarkup, Message, ReplyKeyboardMarkup,
                           ReplyKeyboardRemove, Update, User)

BOT_ID = 999
BOT_USERNAME = "test_bot"
BOT_TOKEN = "12345:DUMMY"


# ── запись одного вызова Bot API ─────────────────────────────────────────────

@dataclass
class Call:
    """Один исходящий вызов: head — строка вида (`= edit #1`, `~ …`),
    body — текст HTML или подпись как ушли, markup — клавиатура."""
    head: str
    body: str | None = None
    caption: bool = False          # body — подпись файла (лимит 1024), а не текст (4096)
    toast: str | None = None       # текст всплывашки (лимит 200, без разметки)
    markup: object | None = None


@dataclass
class Record:
    """Результат снимка: что сделал человек (последнее действие) и что бот
    ответил на него (calls), и все вызовы снимка вместе с подготовкой
    (everything) — по ним проверяется, что ни одно нажатие не ушло в
    обработчик устаревшей кнопки."""
    action: str
    calls: list[Call] = field(default_factory=list)
    everything: list[Call] = field(default_factory=list)


class StubSession(BaseSession):
    """Заглушка сессии: каждый метод aiogram записывается и получает
    правдоподобный ответ, прогнанный через тот же разбор, что у настоящей
    сессии (check_response) — объекты ответа привязаны к боту, и обработчик
    может звать на них методы дальше. Неизвестный метод — запись `? Имя` и
    ошибка: новый вызов в коде должен попасть в обвязку осознанно."""

    def __init__(self, chat_id: int, now: _dt.datetime):
        super().__init__()
        self.chat_id = chat_id
        self.date = int(now.timestamp())
        self.calls: list[Call] = []
        self._ids = itertools.count(2)          # #1 — сообщение, с которого начат снимок
        self.texts: dict[int, tuple[str, object]] = {}
        self.nav = 1                            # где сейчас живое меню — на нём жмут дальше

    # ── ответы ────────────────────────────────────────────────────────────

    def _msg(self, mid: int, text: str | None = None, caption: str | None = None, **extra) -> dict:
        m = {"message_id": mid, "date": self.date,
             "chat": {"id": self.chat_id, "type": "private"},
             "from": {"id": BOT_ID, "is_bot": True, "first_name": "Bot", "username": BOT_USERNAME}}
        if text is not None:
            m["text"] = text
        if caption is not None:
            m["caption"] = caption
        m.update(extra)
        return m

    def _remember(self, mid: int, text, markup) -> None:
        self.texts[mid] = (text, markup)
        if isinstance(markup, InlineKeyboardMarkup):
            self.nav = mid

    def _answer(self, method) -> object:
        if isinstance(method, GetMe):
            return {"id": BOT_ID, "is_bot": True, "first_name": "Bot", "username": BOT_USERNAME}
        if isinstance(method, SendMessage):
            mid = next(self._ids)
            self.calls.append(Call(f"+ send #{mid}", method.text, markup=method.reply_markup))
            self._remember(mid, method.text, method.reply_markup)
            return self._msg(mid, text=method.text)
        if isinstance(method, EditMessageText):
            mid = int(method.message_id or 0)
            self.calls.append(Call(f"= edit #{mid}", method.text, markup=method.reply_markup))
            self._remember(mid, method.text, method.reply_markup)
            return self._msg(mid, text=method.text)
        if isinstance(method, EditMessageCaption):
            mid = int(method.message_id or 0)
            self.calls.append(Call(f"= caption #{mid}", method.caption, caption=True,
                                   markup=method.reply_markup))
            return self._msg(mid, caption=method.caption)
        if isinstance(method, EditMessageMedia):
            mid = int(method.message_id or 0)
            cap = getattr(method.media, "caption", None)
            self.calls.append(Call(f"= media #{mid}", cap, caption=True, markup=method.reply_markup))
            return self._msg(mid, caption=cap)
        if isinstance(method, EditMessageReplyMarkup):
            mid = int(method.message_id or 0)
            if method.reply_markup is None or not getattr(method.reply_markup, "inline_keyboard", None):
                self.calls.append(Call(f"- markup off #{mid}"))
            else:
                self.calls.append(Call(f"= markup #{mid}", markup=method.reply_markup))
                self.nav = mid
            return self._msg(mid, text=self.texts.get(mid, ("…", None))[0] or "…")
        if isinstance(method, DeleteMessage):
            self.calls.append(Call(f"- delete #{method.message_id}"))
            return True
        if isinstance(method, DeleteMessages):
            for mid in method.message_ids:
                self.calls.append(Call(f"- delete #{mid}"))
            return True
        if isinstance(method, AnswerCallbackQuery):
            text = method.text or ""
            mark = "~!" if method.show_alert else "~"
            self.calls.append(Call(f"{mark} {text}" if text else f"{mark} (пустой ответ)", toast=text))
            return True
        if isinstance(method, (SendDocument, SendPhoto, SendAnimation)):
            kind = {SendDocument: "document", SendPhoto: "photo", SendAnimation: "animation"}[type(method)]
            mid = next(self._ids)
            self.calls.append(Call(f"+ {kind} #{mid}", method.caption, caption=True,
                                   markup=method.reply_markup))
            self._remember(mid, method.caption, method.reply_markup)
            return self._msg(mid, caption=method.caption)
        if isinstance(method, SendMediaGroup):
            out = []
            for item in method.media:
                mid = next(self._ids)
                cap = getattr(item, "caption", None)
                self.calls.append(Call(f"+ media #{mid}", cap, caption=True))
                out.append(self._msg(mid, caption=cap))
            return out
        if isinstance(method, (SetMyCommands, DeleteMyCommands, SetChatMenuButton)):
            return True
        self.calls.append(Call(f"? {type(method).__name__}"))
        raise AssertionError(f"обвязка не знает метод {type(method).__name__} — добавь его в StubSession")

    async def make_request(self, bot, method, timeout=None):
        result = self._answer(method)
        content = json.dumps({"ok": True, "result": result}, ensure_ascii=False)
        return self.check_response(bot=bot, method=method, status_code=200, content=content).result

    async def stream_content(self, *a, **k):                # pragma: no cover
        raise AssertionError("скачиваний в эталонах нет")
        yield b""

    async def close(self):
        pass


def make_bot(session: StubSession) -> Bot:
    """Бот, как в runtime/main.py: HTML по умолчанию, без превью ссылок."""
    return Bot(BOT_TOKEN, session=session,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))


# ── построитель событий ──────────────────────────────────────────────────────

def _user(uid: int, name: str) -> User:
    return User(id=uid, is_bot=False, first_name=name)


def _chat(uid: int) -> Chat:
    return Chat(id=uid, type="private")


def packed(cb) -> str:
    return cb if isinstance(cb, str) else cb.pack()


def callback_update(update_id: int, session: StubSession, uid: int, name: str, data) -> Update:
    """Нажатие кнопки на текущем живом меню снимка (#1 или последнее
    сообщение с кнопками)."""
    mid = session.nav
    text = (session.texts.get(mid, ("…", None))[0]) or "…"
    msg = Message(message_id=mid, date=session.date, chat=_chat(uid),
                  from_user=User(id=BOT_ID, is_bot=True, first_name="Bot", username=BOT_USERNAME),
                  text=text)
    cq = CallbackQuery(id=f"cq{update_id}", from_user=_user(uid, name), chat_instance="ci",
                       message=msg, data=packed(data))
    return Update(update_id=update_id, callback_query=cq)


def message_update(update_id: int, session: StubSession, uid: int, name: str, text: str,
                   message_id: int) -> Update:
    """Сообщение человека (команда или ввод)."""
    entities = None
    if text.startswith("/"):
        cmd = text.split(maxsplit=1)[0]
        entities = [{"type": "bot_command", "offset": 0, "length": len(cmd)}]
    msg = Message(message_id=message_id, date=session.date, chat=_chat(uid),
                  from_user=_user(uid, name), text=text, entities=entities)
    return Update(update_id=update_id, message=msg)


async def run(dp, session: StubSession, *, uid: int, name: str, start: str | None,
              press: list, text: str | None) -> Record:
    """Прогнать действия снимка через диспетчер; вернуть запись последнего."""
    bot = make_bot(session)
    steps: list[tuple[str, object]] = []
    if start is not None:
        steps.append(("start", start))
    steps += [("press", p) for p in press]
    if text is not None:
        steps.append(("text", text))
    assert steps, "у снимка нет ни одного действия"
    rec = Record(action="")
    user_mid = 1          # сообщение человека в начале снимка — #1; дальше — после ответов бота
    for i, (kind, val) in enumerate(steps, start=1):
        before = len(session.calls)
        if kind == "press":
            upd = callback_update(i, session, uid, name, val)
            rec.action = f"> press {packed(val)}"
        else:
            body = f"/start {val}".rstrip() if kind == "start" else str(val)
            if i > 1:
                user_mid = next(session._ids)
            upd = message_update(i, session, uid, name, body, user_mid)
            rec.action = f"> {body}" if kind == "start" else f"> text «{val}»"
        await dp.feed_update(bot, upd)
        rec.calls = session.calls[before:]
    rec.everything = list(session.calls)
    return rec


# ── сериализация ─────────────────────────────────────────────────────────────

def _button(b) -> str:
    if b.callback_data is not None:
        target = b.callback_data
    elif b.url is not None:
        target = b.url
    elif getattr(b, "copy_text", None) is not None:
        target = f"copy:{b.copy_text.text}"
    else:
        target = "?"
    red = " | красная" if getattr(b, "style", None) == "danger" else ""
    return f"[{b.text} | {target}{red}]"


def markup_lines(markup) -> list[str]:
    if markup is None:
        return []
    if isinstance(markup, InlineKeyboardMarkup):
        return ["  " + " ".join(_button(b) for b in row) for row in markup.inline_keyboard]
    if isinstance(markup, ReplyKeyboardMarkup):
        return ["  reply: " + " ".join(f"[{b.text}]" for b in row) for row in markup.keyboard]
    if isinstance(markup, ReplyKeyboardRemove):
        return ["  reply: (снята)"]
    return [f"  ? {type(markup).__name__}"]


def serialize(shot_id: str, title: str, rec: Record) -> str:
    out = [f"### {shot_id}" + (f" — {title}" if title else ""), rec.action]
    for c in rec.calls:
        out.append(c.head)
        if c.body is not None and not c.head.startswith("~"):
            out.extend(c.body.split("\n"))
        out.extend(markup_lines(c.markup))
    return "\n".join(out) + "\n"


# ── проверки ограничений Telegram и разметки (§ «Ограничения Telegram») ─────

TEXT_MAX, CAPTION_MAX, TOAST_MAX, CB_MAX, ROWS_MAX, LABEL_MAX = 4096, 1024, 200, 64, 10, 18
ALLOWED = {"b": set(), "i": set(), "u": set(), "s": set(), "code": {"class"}, "pre": set(),
           "a": {"href"}, "blockquote": {"expandable"}, "tg-spoiler": set()}
_TAG = re.compile(r'<(/?)([a-z][a-z-]*)((?:\s+[a-z-]+(?:="[^"<>]*")?)*)\s*>')
_ATTR = re.compile(r'([a-z-]+)(?:="[^"<>]*")?')
_ENTITY = re.compile(r'&(?:lt|gt|amp|quot|#\d+|#x[0-9a-fA-F]+);')
_START = re.compile(rf'href="https://t\.me/{BOT_USERNAME}\?start=([^"]*)"')
_PAYLOAD = re.compile(r'[A-Za-z0-9_-]{1,64}')


def html_problems(text: str) -> list[str]:
    """Разметка, которую Telegram не примет: тег вне списка, атрибут вне
    списка, «<» или «&» не тегом и не сущностью, несбалансированные теги."""
    out: list[str] = []
    stack: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "<":
            m = _TAG.match(text, i)
            if m is None:
                out.append(f"неэкранированный «<» на позиции {i}: {text[i:i + 20]!r}")
                i += 1
                continue
            closing, name, attrs = m.group(1), m.group(2), m.group(3)
            if name not in ALLOWED:
                out.append(f"тег <{name}> вне разрешённых")
            elif closing:
                if attrs.strip():
                    out.append(f"атрибуты у закрывающего </{name}>")
                if not stack or stack[-1] != name:
                    out.append(f"</{name}> без пары (открыты: {stack})")
                else:
                    stack.pop()
            else:
                for a in _ATTR.findall(attrs):
                    if a not in ALLOWED[name]:
                        out.append(f"атрибут {a} у <{name}>")
                stack.append(name)
            i = m.end()
            continue
        if ch == "&":
            m = _ENTITY.match(text, i)
            if m is None:
                out.append(f"неэкранированный «&» на позиции {i}: {text[i:i + 20]!r}")
                i += 1
                continue
            i = m.end()
            continue
        i += 1
    if stack:
        out.append(f"незакрытые теги: {stack}")
    return out


def plain(text: str) -> str:
    """Текст после разбора разметки — по нему Telegram считает длину."""
    return _html.unescape(_TAG.sub("", text))


def problems(shot_id: str, rec: Record, label_exceptions: dict[str, set[str]]) -> list[str]:
    """Нарушения ограничений Telegram и правил проекта в записи снимка.
    label_exceptions — {id снимка: подписи}, которым длинная подпись в ряду
    разрешена макетом."""
    out: list[str] = []
    allowed_long = label_exceptions.get(shot_id, set())
    for c in rec.calls:
        where = f"{shot_id} {c.head}"
        if c.toast is not None:
            if len(c.toast) > TOAST_MAX:
                out.append(f"{where}: всплывашка {len(c.toast)} > {TOAST_MAX}")
            if _TAG.search(c.toast) or _ENTITY.search(c.toast):
                out.append(f"{where}: разметка во всплывашке (Telegram покажет её буквально)")
        if c.body is not None:
            limit = CAPTION_MAX if c.caption else TEXT_MAX
            n = len(plain(c.body))
            if n > limit or (not c.caption and n == 0):
                out.append(f"{where}: длина {n} вне 1…{limit}")
            out += [f"{where}: {p}" for p in html_problems(c.body)]
            for payload in _START.findall(c.body):
                if not _PAYLOAD.fullmatch(payload):
                    out.append(f"{where}: payload /start {payload!r} вне [A-Za-z0-9_-]{{1,64}}")
        if isinstance(c.markup, InlineKeyboardMarkup):
            rows = c.markup.inline_keyboard
            if len(rows) > ROWS_MAX:
                out.append(f"{where}: рядов {len(rows)} > {ROWS_MAX}")
            for row in rows:
                for b in row:
                    if b.callback_data is not None and not 1 <= len(b.callback_data.encode()) <= CB_MAX:
                        out.append(f"{where}: callback_data {b.callback_data!r} — "
                                   f"{len(b.callback_data.encode())} байт вне 1…{CB_MAX}")
                    if len(row) >= 2 and len(b.text) > LABEL_MAX and b.text not in allowed_long:
                        out.append(f"{where}: подпись «{b.text}» ({len(b.text)}) длиннее "
                                   f"{LABEL_MAX} в ряду из {len(row)}")
    return out


def long_labels(rec: Record) -> set[str]:
    """Длинные подписи в рядах из 2+ — для сторожа отживших исключений."""
    return {b.text for c in rec.calls if isinstance(c.markup, InlineKeyboardMarkup)
            for row in c.markup.inline_keyboard if len(row) >= 2
            for b in row if len(b.text) > LABEL_MAX}


__all__ = ["StubSession", "Record", "Call", "run", "serialize", "problems", "html_problems",
           "plain", "long_labels", "packed", "CallbackData", "BOT_USERNAME"]
