"""Обвязка эталонов экранов: настоящий диспетчер роли на заглушке сессии Bot API.

Снимок гонится так же, как живое действие: событие (колбэк, /start, текст,
файл) уходит в `make_dispatcher(role, …)` — AccessMiddleware, фильтры
роутеров, разбор упакованного колбэка, — а всё, что бот после этого сказал
Telegram, ловит заглушка сессии и пишет по порядку. Событие без действия
человека (уведомление, итог после рестарта) — шаг `call`: функция получает
сервисы и бота снимка. Запись сериализуется в текст эталона; рядом —
проверки ограничений Telegram и разметки HTML по каждому снимку.

Формат записи:

    ### <id> — <заголовок>
    > press <колбэк> | > /start <payload> | > text «…» | > file <имя> | > call <имя>
    ~ всплывашка  (~! — alert;  ~ (пустой ответ);  ~ (повторный ответ))
    = edit #N | + send #N | + document #N | - delete #N | - markup off #N
    ! правка удалённого #N
    <текст HTML как ушёл>
      [подпись | callback_data] [подпись | url] [подпись | copy:текст] …

Вызов в чат, отличный от чата снимка, помечен в заголовке: `+ send #1 → чат
админ`. Нумерация сообщений — своя в каждом чате снимка: сообщение, с
которого снимок начат (нажатая кнопка или сообщение человека), — #1 в чате
снимка, новые — дальше. Подготовительные шаги выполняются, но не печатаются —
только последний.
"""
from __future__ import annotations

import copy
import datetime as _dt
import html as _html
import inspect
import itertools
import json
import pathlib
import re
import shutil
from dataclasses import dataclass, field

import pytest

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.filters.callback_data import CallbackData
from aiogram.methods import (AnswerCallbackQuery, DeleteMessage, DeleteMessages, EditMessageCaption,
                             EditMessageMedia, EditMessageReplyMarkup, EditMessageText, GetFile, GetMe,
                             SendAnimation, SendDocument, SendMediaGroup, SendMessage, SendPhoto,
                             SetMyCommands, DeleteMyCommands, SetChatMenuButton)
from aiogram.types import (CallbackQuery, Chat, InlineKeyboardMarkup, Message, ReplyKeyboardMarkup,
                           ReplyKeyboardRemove, Update, User)

from awgbot.bot import paging
from awgbot.bot.handlers import common
from awgbot.bot.routers import make_dispatcher
from awgbot.core import access_cache, config, settings
from tests.conftest import _REPO_CONF
from tests.screens import base

BOT_ID = 999
BOT_USERNAME = "test_bot"
BOT_TOKEN = "12345:DUMMY"
_PHOTO = [{"file_id": "DUMMYPHOTO", "file_unique_id": "DUMMYPHOTO", "width": 1, "height": 1}]


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
    """Результат снимка: последнее действие (action), что бот ответил на
    него (calls), и все вызовы снимка вместе с подготовкой (everything) — по
    ним проверяется, что ни одно нажатие не ушло в обработчик устаревшей
    кнопки."""
    action: str
    calls: list[Call] = field(default_factory=list)
    everything: list[Call] = field(default_factory=list)


class StubSession(BaseSession):
    """Заглушка сессии: каждый метод aiogram записывается и получает
    правдоподобный ответ, прогнанный через тот же разбор, что у настоящей
    сессии (check_response) — объекты ответа привязаны к боту, и обработчик
    может звать на них методы дальше. Неизвестный метод — запись `? Имя` и
    ошибка: новый вызов в коде должен попасть в обвязку осознанно.

    Помнит по чатам: номера сообщений, что в них лежит (текст или фото),
    удалённые сообщения и отвеченные колбэки — правка удалённого и второй
    ответ на колбэк в Telegram молча проваливаются, в записи они видны.
    peers — подписи чатов-адресатов ({tg_id: «админ»}).
    """

    def __init__(self, chat_id: int, now: _dt.datetime, peers: dict | None = None):
        super().__init__()
        self.chat_id = int(chat_id)
        self.date = int(now.timestamp())
        self.peers = dict(peers or {})
        self.calls: list[Call] = []
        self._ids: dict[int, itertools.count] = {self.chat_id: itertools.count(2)}
        self.messages: dict[tuple[int, int], dict] = {}     # (чат, #) → что лежит
        self.deleted: set[tuple[int, int]] = set()
        self.answered: set[str] = set()
        self.files: dict[str, bytes] = {}                   # file_id → байты для download
        self.nav = 1                                        # живое меню в чате снимка

    # ── память ────────────────────────────────────────────────────────────

    def next_id(self, chat: int | None = None) -> int:
        chat = self.chat_id if chat is None else int(chat)
        return next(self._ids.setdefault(chat, itertools.count(1)))

    def remember(self, chat: int, mid: int, *, text=None, caption=None, photo=False, markup=None) -> None:
        self.messages[(chat, mid)] = {"text": text, "caption": caption, "photo": photo}
        if chat == self.chat_id and isinstance(markup, InlineKeyboardMarkup):
            self.nav = mid

    def _to(self, chat) -> str:
        chat = int(chat)
        if chat == self.chat_id:
            return ""
        return f" → чат {self.peers.get(chat, f'tg {chat}')}"

    def _msg(self, chat: int, mid: int, **extra) -> dict:
        m = {"message_id": mid, "date": self.date, "chat": {"id": chat, "type": "private"},
             "from": {"id": BOT_ID, "is_bot": True, "first_name": "Bot", "username": BOT_USERNAME}}
        m.update({k: v for k, v in extra.items() if v is not None})
        return m

    def _known(self, chat: int, mid: int) -> dict:
        """Ответ Telegram на правку: сообщение как оно лежит (или «…»)."""
        have = self.messages.get((chat, mid), {})
        if have.get("photo"):
            return self._msg(chat, mid, photo=_PHOTO, caption=have.get("caption"))
        return self._msg(chat, mid, text=have.get("text") or "…")

    def _edited(self, chat: int, mid: int) -> None:
        if (chat, mid) in self.deleted:
            self.calls.append(Call(f"! правка удалённого #{mid}{self._to(chat)}"))

    # ── ответы ────────────────────────────────────────────────────────────

    def _answer(self, method) -> object:
        chat = getattr(method, "chat_id", None)
        chat = self.chat_id if chat is None else int(chat)
        to = self._to(chat)
        if isinstance(method, GetMe):
            return {"id": BOT_ID, "is_bot": True, "first_name": "Bot", "username": BOT_USERNAME}
        if isinstance(method, GetFile):
            data = self.files.get(method.file_id)
            assert data is not None, f"файла {method.file_id} в снимке нет"
            return {"file_id": method.file_id, "file_unique_id": method.file_id,
                    "file_size": len(data), "file_path": f"documents/{method.file_id}"}
        if isinstance(method, SendMessage):
            mid = self.next_id(chat)
            self.calls.append(Call(f"+ send #{mid}{to}", method.text, markup=method.reply_markup))
            self.remember(chat, mid, text=method.text, markup=method.reply_markup)
            return self._msg(chat, mid, text=method.text)
        if isinstance(method, EditMessageText):
            mid = int(method.message_id or 0)
            self._edited(chat, mid)
            self.calls.append(Call(f"= edit #{mid}{to}", method.text, markup=method.reply_markup))
            self.remember(chat, mid, text=method.text, markup=method.reply_markup)
            return self._msg(chat, mid, text=method.text)
        if isinstance(method, EditMessageCaption):
            mid = int(method.message_id or 0)
            self._edited(chat, mid)
            self.calls.append(Call(f"= caption #{mid}{to}", method.caption, caption=True,
                                   markup=method.reply_markup))
            self.remember(chat, mid, caption=method.caption, photo=True, markup=method.reply_markup)
            return self._msg(chat, mid, caption=method.caption, photo=_PHOTO)
        if isinstance(method, EditMessageMedia):
            mid = int(method.message_id or 0)
            self._edited(chat, mid)
            cap = getattr(method.media, "caption", None)
            self.calls.append(Call(f"= media #{mid}{to}", cap, caption=True, markup=method.reply_markup))
            self.remember(chat, mid, caption=cap, photo=True, markup=method.reply_markup)
            return self._msg(chat, mid, caption=cap, photo=_PHOTO)
        if isinstance(method, EditMessageReplyMarkup):
            mid = int(method.message_id or 0)
            self._edited(chat, mid)
            if method.reply_markup is None or not getattr(method.reply_markup, "inline_keyboard", None):
                self.calls.append(Call(f"- markup off #{mid}{to}"))
            else:
                self.calls.append(Call(f"= markup #{mid}{to}", markup=method.reply_markup))
                if chat == self.chat_id:
                    self.nav = mid
            return self._known(chat, mid)
        if isinstance(method, DeleteMessage):
            self.calls.append(Call(f"- delete #{method.message_id}{to}"))
            self.deleted.add((chat, int(method.message_id)))
            return True
        if isinstance(method, DeleteMessages):
            for mid in method.message_ids:
                self.calls.append(Call(f"- delete #{mid}{to}"))
                self.deleted.add((chat, int(mid)))
            return True
        if isinstance(method, AnswerCallbackQuery):
            text = method.text or ""
            if method.callback_query_id in self.answered:
                self.calls.append(Call("~ (повторный ответ)" + (f" {text}" if text else "")))
                return True
            self.answered.add(method.callback_query_id)
            mark = "~!" if method.show_alert else "~"
            self.calls.append(Call(f"{mark} {text}" if text else f"{mark} (пустой ответ)", toast=text))
            return True
        if isinstance(method, (SendDocument, SendPhoto, SendAnimation)):
            kind = {SendDocument: "document", SendPhoto: "photo", SendAnimation: "animation"}[type(method)]
            mid = self.next_id(chat)
            self.calls.append(Call(f"+ {kind} #{mid}{to}", method.caption, caption=True,
                                   markup=method.reply_markup))
            self.remember(chat, mid, caption=method.caption, photo=kind == "photo", markup=method.reply_markup)
            return self._msg(chat, mid, caption=method.caption, photo=_PHOTO if kind == "photo" else None,
                             document=({"file_id": f"DUMMYDOC{mid}", "file_unique_id": f"DUMMYDOC{mid}"}
                                       if kind == "document" else None))
        if isinstance(method, SendMediaGroup):
            out = []
            for item in method.media:
                mid = self.next_id(chat)
                cap = getattr(item, "caption", None)
                self.calls.append(Call(f"+ media #{mid}{to}", cap, caption=True))
                self.remember(chat, mid, caption=cap, photo=True)
                out.append(self._msg(chat, mid, caption=cap, photo=_PHOTO))
            return out
        if isinstance(method, (SetMyCommands, DeleteMyCommands, SetChatMenuButton)):
            return True
        self.calls.append(Call(f"? {type(method).__name__}"))
        raise AssertionError(f"обвязка не знает метод {type(method).__name__} — добавь его в StubSession")

    async def make_request(self, bot, method, timeout=None):
        result = self._answer(method)
        content = json.dumps({"ok": True, "result": result}, ensure_ascii=False)
        return self.check_response(bot=bot, method=method, status_code=200, content=content).result

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        """Скачивание файла, присланного шагом `file`: байты по file_id из пути."""
        file_id = str(url).rsplit("/", 1)[-1]
        data = self.files.get(file_id)
        assert data is not None, f"скачивание неизвестного файла {url}"
        for i in range(0, len(data), chunk_size) or [0]:
            yield data[i:i + chunk_size]

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
    """Нажатие кнопки на живом меню чата снимка (#1 или последнее сообщение с
    кнопками); под фото — сообщение с фото и подписью, как его видит
    обработчик (cb.message.photo)."""
    mid = session.nav
    have = session.messages.get((session.chat_id, mid), {})
    bot_user = User(id=BOT_ID, is_bot=True, first_name="Bot", username=BOT_USERNAME)
    if have.get("photo"):
        msg = Message(message_id=mid, date=session.date, chat=_chat(uid), from_user=bot_user,
                      photo=_PHOTO, caption=have.get("caption") or "…")
    else:
        msg = Message(message_id=mid, date=session.date, chat=_chat(uid), from_user=bot_user,
                      text=have.get("text") or "…")
    cq = CallbackQuery(id=f"cq{update_id}", from_user=_user(uid, name), chat_instance="ci",
                       message=msg, data=packed(data))
    return Update(update_id=update_id, callback_query=cq)


def message_update(update_id: int, session: StubSession, uid: int, name: str, text: str | None,
                   message_id: int, *, entities=None, document=None, caption=None) -> Update:
    """Сообщение человека: команда, ввод (с разметкой — entities) или файл."""
    if text is not None and text.startswith("/") and entities is None:
        cmd = text.split(maxsplit=1)[0]
        entities = [{"type": "bot_command", "offset": 0, "length": len(cmd)}]
    msg = Message(message_id=message_id, date=session.date, chat=_chat(uid),
                  from_user=_user(uid, name), text=text, entities=entities,
                  document=document, caption=caption)
    return Update(update_id=update_id, message=msg)


# ── шаги снимка ──────────────────────────────────────────────────────────────

STEP_KINDS = ("start", "press", "text", "file", "call")


def check_step(step) -> tuple:
    """Шаг — кортеж: ("start", payload) | ("press", колбэк) | ("text", ввод[,
    entities]) | ("file", имя, байты[, подпись]) | ("call", функция[, имя])."""
    assert isinstance(step, tuple) and step and step[0] in STEP_KINDS, f"шаг не понят: {step!r}"
    kind, n = step[0], len(step)
    assert {"start": (2,), "press": (2,), "text": (2, 3), "file": (3, 4), "call": (2, 3)}[kind].count(n), \
        f"шаг {kind}: неверное число полей — {step!r}"
    if kind == "file":
        assert isinstance(step[2], (bytes, bytearray)), f"файл {step[1]}: байты, а не {type(step[2]).__name__}"
    if kind == "call":
        assert callable(step[1]), f"call: не функция — {step!r}"
    return step


def _call_name(step) -> str:
    if len(step) == 3:
        return step[2]
    name = getattr(step[1], "__name__", "")
    return name if name and name != "<lambda>" else "событие"


async def run(dp, session: StubSession, *, uid: int, name: str, steps, services=None,
              photo: bool = False) -> Record:
    """Прогнать шаги снимка через диспетчер; вернуть запись последнего.
    photo — сообщение #1, под которым жмут кнопку, — фото с подписью."""
    bot = make_bot(session)
    steps = [check_step(s) for s in steps]
    assert steps, "у снимка нет ни одного шага"
    if photo:
        session.remember(session.chat_id, 1, caption="…", photo=True)
    rec = Record(action="")
    first_user_msg = True       # сообщение человека в начале снимка — #1, дальше — по счётчику
    for i, step in enumerate(steps, start=1):
        kind = step[0]
        before = len(session.calls)
        if kind == "call":
            rec.action = f"> call {_call_name(step)}"
            await step[1](services, bot)
        elif kind == "press":
            rec.action = f"> press {packed(step[1])}"
            await dp.feed_update(bot, callback_update(i, session, uid, name, step[1]))
        else:
            mid = 1 if (first_user_msg and i == 1) else session.next_id()
            first_user_msg = False
            if kind == "start":
                body = f"/start {step[1]}".rstrip()
                upd = message_update(i, session, uid, name, body, mid)
                rec.action = f"> {body}"
            elif kind == "text":
                upd = message_update(i, session, uid, name, step[1], mid,
                                     entities=step[2] if len(step) == 3 else None)
                shown = upd.message.html_text if len(step) == 3 else step[1]
                rec.action = f"> text «{shown}»"
            else:
                fname, data = step[1], bytes(step[2])
                file_id = f"DUMMYFILE{i}"
                session.files[file_id] = data
                cap = step[3] if len(step) == 4 else None
                doc = {"file_id": file_id, "file_unique_id": file_id, "file_name": fname,
                       "file_size": len(data)}
                upd = message_update(i, session, uid, name, None, mid, document=doc, caption=cap)
                rec.action = f"> file {fname}" + (f" «{cap}»" if cap else "")
            await dp.feed_update(bot, upd)
        rec.calls = session.calls[before:]
    rec.everything = list(session.calls)
    return rec



# ── снимок целиком ───────────────────────────────────────────────────────────

def _fresh_conf(dst: pathlib.Path, overrides: dict) -> None:
    """Своя копия conf/ репозитория на снимок: копия общего прогона живёт весь
    процесс, и тест, записавший туда настройку раньше, менял бы эталон."""
    dst.mkdir()
    for f in _REPO_CONF.glob("*.yaml"):
        shutil.copy2(f, dst / f.name)
    settings.init(dst)
    for k, v in overrides.items():
        settings.set_value(k, v)


def _services(role: str, db, mp):
    if role == "gateway":
        from awgbot.domain.gateway import GatewayServices
        from awgbot.runtime import linkclient
        mp.setattr(config, "ROLE", "gateway")
        mp.setattr(linkclient, "enabled", lambda: False)
        return GatewayServices(db)
    from awgbot.domain.services import Services
    mp.setattr(config, "ROLE", "client")
    return Services(db)


def _reset_module_state(mp) -> None:
    """Модульное состояние процесса, которое иначе протекало бы из снимка в
    снимок (и из чужих тестов того же воркера): страницы листания, «карточка
    с главной», кэш «кто это» в middleware, метки и замки объявлений, клиент
    упр. канала, запасной канал почты, кэши проб и резолва."""
    from awgbot.bot import notifier
    from awgbot.bot.handlers.admin import broadcast
    from awgbot.infra import awg, nftguard
    from awgbot.infra.routing import probes, selfcheck
    from awgbot.runtime import linkclient
    paging._pages.clear()
    common._card_home.clear()
    access_cache.invalidate_all()
    broadcast._last_broadcast_at.clear()
    broadcast._bc_locks.clear()
    broadcast._bc_render_tasks.clear()
    nftguard._resolve_cache.clear()
    awg._params_cache.clear()
    awg._params_cached_at.clear()
    probes._last_probe_ms.clear()
    mp.setattr(selfcheck, "_selfcheck_cache", None)
    mp.setattr(linkclient, "_client", None)
    mp.setattr(linkclient, "_notify", None)
    mp.setattr(notifier, "_email_fallback", None)


def run_builder(shot: base.Shot, services, mp):
    """Построитель снимка: (services) или (services, monkeypatch снимка)."""
    if shot.data is None:
        return config.ADMIN_ID, "Админ"
    positional = [p for p in inspect.signature(shot.data).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)]
    return shot.data(services, mp) if len(positional) >= 2 else shot.data(services)


async def take(shot: base.Shot, tmp: pathlib.Path, fakes=()) -> Record:
    """Снять один снимок: свежие conf и БД, фейки conftest и модульное
    состояние — в исходное, свой monkeypatch (откатывается в конце снимка,
    поэтому подмены построителя не доживают до следующего), диспетчер роли,
    шаги, запись."""
    from awgbot.infra.db import Database
    for ns, initial in fakes:
        vars(ns).clear()
        vars(ns).update(copy.deepcopy(initial))
    root = tmp / shot.id
    root.mkdir()
    _fresh_conf(root / "conf", shot.conf)
    db = Database(str(root / "bot.db"))
    db.init_schema()
    with pytest.MonkeyPatch.context() as mp:
        _reset_module_state(mp)
        try:
            services = _services(shot.role, db, mp)
            services.bot_username = BOT_USERNAME
            uid, name = run_builder(shot, services, mp)
            access_cache.invalidate_all()
            dp = make_dispatcher(base.DISPATCHER[shot.role], services, db, reattach=True)
            session = StubSession(uid, base.NOW, base.PEERS)
            return await run(dp, session, uid=uid, name=name, steps=shot.all_steps(),
                                     services=services, photo=shot.photo)
        finally:
            db.close()


# ── полнота: какие построители клавиатур вызваны ────────────────────────────

KEYBOARD_MODULES = ("admin", "broadcast", "client", "common", "gateway", "routing", "settings")


def keyboard_builders() -> dict[str, object]:
    """Публичные построители клавиатур: функции модулей awgbot/bot/keyboards/*
    без подчёркивания, объявленные в своём модуле (реэкспорт пакета не
    считается дважды) и возвращающие InlineKeyboardMarkup/ReplyKeyboardMarkup
    по аннотации. Ключ — «модуль.имя»."""
    import importlib
    out = {}
    for short in KEYBOARD_MODULES:
        mod = importlib.import_module(f"awgbot.bot.keyboards.{short}")
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if fn.__module__ != mod.__name__ or name.startswith("_"):
                continue
            if "KeyboardMarkup" in str(fn.__annotations__.get("return", "")):
                out[f"{short}.{name}"] = fn
    out.update(section_builders())
    return out


def section_builders() -> dict[str, object]:
    """Построители клавиатур общих разделов (awgbot/bot/sections/*): по
    имени — keyboard и *_kb, объявленные в модуле раздела. Ключ —
    «sections.<модуль>.<имя>»."""
    from awgbot.bot import sections
    out = {}
    for m in sections.MODULES:
        short = m.__name__.rsplit(".", 1)[-1]
        for name, fn in inspect.getmembers(m, inspect.isfunction):
            if fn.__module__ == m.__name__ and (name == "keyboard" or name.endswith("_kb")):
                out[f"sections.{short}.{name}"] = fn
    return out


def record_section_screens(mp) -> set[tuple[str, str]]:
    """Подменить screen каждого модуля общих разделов записью «(роль словаря,
    раздел)» — каким ролям раздел был нарисован снимками; возвращает
    множество, которое пополняется по ходу."""
    from awgbot.bot import sections
    seen: set[tuple[str, str]] = set()
    for m in sections.MODULES:
        def _wrap(real, sec=m.ID):
            async def screen(br, services, key: str = ""):
                seen.add((br.name, sec))
                return await real(br, services, key)
            return screen
        mp.setattr(m, "screen", _wrap(m.screen))
    return seen


def unannotated_keyboard_functions() -> list[str]:
    """Публичные функции модулей клавиатур без аннотации результата: по ним
    не понять, построитель ли это, — сторож полноты такую пропустил бы."""
    import importlib
    out = []
    for short in KEYBOARD_MODULES:
        mod = importlib.import_module(f"awgbot.bot.keyboards.{short}")
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if fn.__module__ == mod.__name__ and not name.startswith("_") and "return" not in fn.__annotations__:
                out.append(f"{short}.{name}")
    return out


class BuilderCalls:
    """Какие из функций были вызваны внутри `with`: sys.monitoring (PY_START)
    на их кодовых объектах — во всех потоках (обработчики зовут построители и
    через asyncio.to_thread), без накладных расходов на остальной код;
    после первого вызова событие на этой функции гасится."""

    def __init__(self, functions: dict[str, object]):
        self.codes = {fn.__code__: key for key, fn in functions.items()}
        self.seen: set[str] = set()
        self._tool = None

    def __enter__(self):
        import sys
        mon = sys.monitoring
        self._tool = next(i for i in (4, 3, 2, 0) if mon.get_tool(i) is None)
        mon.use_tool_id(self._tool, "screens-keyboards")

        def on_start(code, offset):
            key = self.codes.get(code)
            if key is not None:
                self.seen.add(key)
            return mon.DISABLE
        mon.register_callback(self._tool, mon.events.PY_START, on_start)
        for code in self.codes:
            mon.set_local_events(self._tool, code, mon.events.PY_START)
        return self

    def __exit__(self, *exc):
        import sys
        mon = sys.monitoring
        for code in self.codes:
            mon.set_local_events(self._tool, code, 0)
        mon.register_callback(self._tool, mon.events.PY_START, None)
        mon.free_tool_id(self._tool)
        return False


# ── сериализация ─────────────────────────────────────────────────────────────

def _button(b) -> str:
    if b.callback_data is not None:
        target = b.callback_data
    elif b.url is not None:
        target = b.url
    elif getattr(b, "copy_text", None) is not None:
        # многострочный текст копирования — одной строкой: формат эталона построчный
        target = "copy:" + b.copy_text.text.replace("\n", "\\n")
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


__all__ = ["keyboard_builders", "unannotated_keyboard_functions", "BuilderCalls", "StubSession", "Record", "Call", "run", "take", "run_builder", "check_step", "serialize",
           "problems", "html_problems", "plain", "long_labels", "packed", "CallbackData", "BOT_USERNAME"]
