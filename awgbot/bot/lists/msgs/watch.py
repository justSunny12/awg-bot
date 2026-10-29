"""watch.py — часть пакета lists/msgs: посредник запросов сессии бота.

Видит SendMessage, EditMessageText, EditMessageReplyMarkup и DeleteMessage
(прочее пропускает без чтения базы); отказ Telegram уходит наверх как есть,
запись не трогается. После удачного ответа:

  отправка / правка текста  — ссылки на снимки (scan.keys), клавиатура,
                              итог из NOTE (только если текст с него
                              начинается) → list_msg_put; привязалась новая
                              ссылка → правка чата сейчас (heal.heal_chat:
                              сообщения с мёртвыми ключами — вытесненные);
  правка клавиатуры         — list_msg_set_kb (ключи и текст — прежние);
  удаление                  — запись и снимки сообщения прочь, без отметки.

Запись — только при разметке HTML (по умолчанию у всех ботов проекта).
Сбой записи в базу — строкой журнала: отправка уже состоялась, ломать её
нельзя. DeleteMessages (пачка) не учитывается: Telegram пропускает
неудаляемые номера молча — разберутся правка ссылки и такт."""
from __future__ import annotations

import logging

from aiogram.client.default import Default
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import DeleteMessage, EditMessageReplyMarkup, EditMessageText, SendMessage
from aiogram.types import Message

from awgbot.util import timeutil

from . import NOTE, OWN, scan

log = logging.getLogger(__name__)

_TEXT = (SendMessage, EditMessageText)


def _html(method) -> bool:
    """Разметка метода — HTML: не задана (Default — по умолчанию бота, у всех ботов
    проекта это HTML) или названа явно."""
    pm = getattr(method, "parse_mode", None)
    if pm is None or isinstance(pm, Default):
        return True
    return str(getattr(pm, "value", pm)).upper() == "HTML"


def _sent_iso(result) -> str:
    date = getattr(result, "date", None) if isinstance(result, Message) else None
    return timeutil.to_iso(date) if date is not None else timeutil.now_iso()


class ListMsgWatch(BaseRequestMiddleware):

    def __init__(self, services):
        self.services = services

    async def __call__(self, make_request, bot, method):
        if not isinstance(method, (SendMessage, EditMessageText, EditMessageReplyMarkup, DeleteMessage)):
            return await make_request(bot, method)
        result = await make_request(bot, method)      # отказ — наверх, запись не трогаем
        if OWN.get():
            return result                              # своя правка (heal): запись обновит он
        try:
            await self._after(bot, method, result)
        except Exception as e:                         # noqa: BLE001 — отправка уже состоялась
            log.warning("lists: запись сообщения не обновлена: %s", e)
        return result

    async def _after(self, bot, method, result) -> None:
        from awgbot.bot.handlers.common import call
        db = self.services.db
        chat = int(method.chat_id)
        if isinstance(method, DeleteMessage):
            keys = await call(db.list_msg_forget, chat, int(method.message_id), True)
            if keys:
                log.info("lists: сообщение %d чата %d удалено — снимки %s сняты", int(method.message_id), chat, keys)
            return
        mid = int(result.message_id) if isinstance(result, Message) else int(getattr(method, "message_id", 0) or 0)
        if not mid:
            return
        kb = scan.kb_json(method.reply_markup)
        if isinstance(method, _TEXT):
            if not _html(method):
                return
            html = method.text or ""
            keys = scan.keys(html)
            note = NOTE.get()
            if note and html.startswith(note + "\n\n"):
                NOTE.set("")
            else:
                note = ""
            if isinstance(method, SendMessage) and not keys and not note and not scan.has_hide(kb):
                return                                 # причин нет — база не трогается
            bound = await call(db.list_msg_put, chat, mid, _sent_iso(result), html, kb, keys, note)
            if bound:
                from . import heal
                await heal.heal_chat(bot, self.services, chat)
            return
        if isinstance(method, EditMessageReplyMarkup):
            await call(db.list_msg_set_kb, chat, mid, _sent_iso(result), kb)
