"""watch.py — часть пакета lists/msgs: посредник запросов сессии бота.

Видит SendMessage, EditMessageText, отправки с подписью (SendPhoto,
SendDocument, SendAnimation, SendVideo), EditMessageCaption,
EditMessageReplyMarkup и DeleteMessage (прочее пропускает без чтения базы);
отказ Telegram уходит наверх как есть; у правки текста или подписи
обработчиком (не heal) отказ «не изменено» запись не трогает, любой другой
(сообщения нет, править нельзя) снимает запись и отпускает её снимки:
обработчик шлёт экран заново новым сообщением, и его ссылки привязывают их;
было живым меню чата — им становится присланное заново (REISSUE: отказ
запоминается в контексте задачи, следующая отправка в тот чат — живое меню;
иначе указатель остался бы на пропавшем сообщении, и следующее меню не
погасило бы присланное заново). После удачного ответа:

  отправка / правка текста  — ссылки на снимки (scan.keys), клавиатура,
                              итог из NOTE (только если текст с него
                              начинается) → list_msg_put; привязалась новая
                              ссылка → правка чата сейчас (heal.heal_chat:
                              сообщения с мёртвыми ключами — вытесненные);
  отправка / правка подписи — то же по подписи; запись с признаком `cap` —
                              правка и гашение идут editMessageCaption;
  правка клавиатуры         — list_msg_set_kb (ключи и текст — прежние);
  удаление                  — запись и снимки сообщения прочь, без отметки.

Запись — только при разметке HTML (по умолчанию у всех ботов проекта).
Сбой записи в базу — строкой журнала: отправка уже состоялась, ломать её
нельзя. DeleteMessages (пачка) не учитывается: Telegram пропускает
неудаляемые номера молча — разберутся правка ссылки и такт. Альбом
(SendMediaGroup) не учитывается: у него нет клавиатуры, а подпись стоит у
первого вложения из нескольких ответных сообщений."""
from __future__ import annotations

import logging

from aiogram.client.default import Default
from aiogram.exceptions import TelegramBadRequest
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.methods import (DeleteMessage, EditMessageCaption, EditMessageReplyMarkup, EditMessageText,
                             SendAnimation, SendDocument, SendMessage, SendPhoto, SendVideo)
from aiogram.types import Message

from awgbot.util import timeutil

from . import NOTE, OWN, REISSUE, scan

log = logging.getLogger(__name__)

_TEXT = (SendMessage, EditMessageText)
_CAP = (SendPhoto, SendDocument, SendAnimation, SendVideo, EditMessageCaption)
_SENDS = (SendMessage, SendPhoto, SendDocument, SendAnimation, SendVideo)
_EDITS = (EditMessageText, EditMessageCaption)


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
        if not isinstance(method, _TEXT + _CAP + (EditMessageReplyMarkup, DeleteMessage)):
            return await make_request(bot, method)
        try:
            result = await make_request(bot, method)
        except TelegramBadRequest as e:                # отказ — наверх
            if isinstance(method, _EDITS) and not OWN.get() and "message is not modified" not in str(e):
                await self._refused(method, e)
            raise
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
        if isinstance(method, _SENDS):
            await self._reissued(chat, mid)
        if isinstance(method, _TEXT + _CAP):
            if not _html(method):
                return
            cap = isinstance(method, _CAP)
            html = (method.caption if cap else method.text) or ""
            keys = scan.keys(html)
            note = NOTE.get()
            if note and html.startswith(note + "\n\n"):
                NOTE.set("")
            else:
                note = ""
            if isinstance(method, _SENDS) and not keys and not note and not scan.has_hide(kb):
                return                                 # причин нет — база не трогается
            sent = _sent_iso(result)
            bound = await call(db.list_msg_put, chat, mid, sent, html, kb, keys, note, cap)
            if bound:
                from . import heal
                await heal.heal_chat(bot, self.services, chat)
            if isinstance(method, _EDITS):
                await self._hide_age(bot, chat, mid, sent, kb)   # отправка всегда свежая — проверка только правке
            return
        if isinstance(method, EditMessageReplyMarkup):
            sent = _sent_iso(result)
            await call(db.list_msg_set_kb, chat, mid, sent, kb)
            await self._hide_age(bot, chat, mid, sent, kb)

    async def _refused(self, method, e) -> None:
        """Правку экрана отклонил Telegram (сообщение удалено, править нельзя): обработчик
        пришлёт экран заново новым сообщением — запись прежнего снимается, снимки
        отпущены (не сняты: их привяжет новое сообщение); было живым меню — живым
        станет присланное заново (REISSUE до следующей отправки в чат)."""
        from awgbot.bot.handlers.common import call
        db = self.services.db
        chat, mid = int(method.chat_id), int(getattr(method, "message_id", 0) or 0)
        if not mid:
            return
        try:
            rec = await call(db.list_msg_get, chat, mid)
            if rec is not None:
                await call(db.list_msg_forget, chat, mid, False)
            nav = await call(db.get_nav_message_id, chat)
        except Exception as err:                       # noqa: BLE001 — отказ уже уходит наверх
            log.warning("lists: запись сообщения %d чата %d не снята после отказа правки: %s", mid, chat, err)
            return
        live = nav is not None and int(nav) == mid
        if live:
            REISSUE.set((chat, mid))
        if rec is None and not live:
            return
        log.info("lists: сообщение %d чата %d — правка отклонена (%s): %s%s", mid, chat, e,
                 "запись снята, снимки %s отпущены" % [str(k) for k in (rec.get("keys") or [])]
                 if rec is not None else "записи не было",
                 "; живое меню — им станет присланное заново" if live else "")

    async def _reissued(self, chat: int, mid: int) -> None:
        """Отправка после отказа правки живого меню той же задачей: присланный заново
        экран — живое меню чата (следующее меню погасит его, ссылка на экран правит его)."""
        pend = REISSUE.get()
        if pend is None:
            return
        REISSUE.set(None)
        if pend[0] != chat:
            return
        from awgbot.bot.handlers.common import call
        await call(self.services.db.nav_touch, chat, mid)
        log.info("lists: экран чата %d прислан заново (%d вместо %d) — живое меню теперь он", chat, mid, pend[1])

    async def _hide_age(self, bot, chat: int, mid: int, sent: str, kb: str) -> None:
        """«Скрыть» поставили правкой на старое сообщение (след переезда на меню
        трёхдневной давности, «⬅️ В меню» на итоге спустя выходные): кнопка, которую
        такт не успеет снять до предела удаления, в чате не стоит — снять сразу той
        же задачей. Моложе — стоит до такта, как у всех."""
        from awgbot.infra.db.listsnap import hide_hopeless
        if scan.has_hide(kb) and hide_hopeless({"sent": sent}):
            from . import heal
            await heal.heal_one(bot, self.services, {"chat": chat, "mid": mid}, in_action=True)
