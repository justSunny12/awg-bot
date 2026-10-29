"""heal.py — часть пакета lists/msgs: правка сообщений по записям.

Одна правка на сообщение: мёртвые ключи — ссылки подписью (scan.strip),
«Скрыть» старше суток — ряд прочь (scan.without_hide); меняется текст —
editMessageText с клавиатурой, какая сейчас (правка без reply_markup сняла
бы её), только кнопки — editMessageReplyMarkup. Удачная правка (и «message is
not modified») обновляет запись сама: посредник (watch) увидит свою же
правку, но при «not modified» ответа нет.

Отказы Telegram: «message to edit not found» — человек удалил сообщение:
снимки и запись прочь без отметки; прочие 400 и 403 — править нечего и не
будет: запись прочь, отметки остаются; 429 — в действии не ждать: повтор
фоновой задачей через retry_after (не дольше RETRY_MAX_S), в такте —
подождать (не дольше WAIT_429_S) и продолжить, второй 429 — остаток чата
следующему такту; сеть, 5xx, 401 — запись остаётся, повторят догон и такт.

Темп: правки одного чата — под замком чата и не чаще одной в секунду; на
бота — общий темп BOT_RATE в секунду (как у рассылки)."""
from __future__ import annotations

import asyncio
import logging

from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
                                TelegramRetryAfter, TelegramServerError, TelegramUnauthorizedError)
from aiogram.types import LinkPreviewOptions

from . import OWN, scan

log = logging.getLogger(__name__)

CHAT_INTERVAL_S = 1.0          # правки в одном чате — не чаще одной в секунду
BOT_RATE = 20                  # правок в секунду на бота (общий темп такта)
PARALLEL_CHATS = 6             # чатов в работе разом на такте
RETRY_MAX_S = 600              # фоновый повтор после 429 в действии — не дольше
WAIT_429_S = 60                # ожидание после 429 на такте — не дольше
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

_locks: dict[int, asyncio.Lock] = {}
_last: dict[int, float] = {}
_retry: dict[tuple[int, int], int] = {}     # (чат, номер) → сколько ждать после 429 (такт читает)


class _Pace:
    """Общий темп: старты правок — не чаще одного за interval."""

    def __init__(self, interval: float):
        self.interval = interval
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = asyncio.get_running_loop().time()
            at = max(now, self._next)
            self._next = at + self.interval
        if at > now:
            await asyncio.sleep(at - now)


def _lock(chat: int) -> asyncio.Lock:
    lock = _locks.get(chat)
    if lock is None:
        lock = _locks[chat] = asyncio.Lock()
    return lock


async def _chat_pace(chat: int) -> None:
    loop = asyncio.get_running_loop()
    at = max(loop.time(), _last.get(chat, 0.0) + CHAT_INTERVAL_S) if chat in _last else loop.time()
    _last[chat] = at
    if at > loop.time():
        await asyncio.sleep(at - loop.time())


def _plan(rec: dict) -> tuple[str, str, bool]:
    """(новый HTML, новая клавиатура, меняется ли текст) по записи к правке."""
    html = str(rec.get("html") or "")
    kb = str(rec.get("kb") or "")
    dead = [str(k) for k in (rec.get("dead") or [])]
    html2 = scan.strip(html, dead) if dead else html
    kb2 = scan.without_hide(kb) if rec.get("hide") else kb
    return html2, kb2, bool(dead) and html2 != html


async def _edit(bot, rec: dict, html2: str, kb2: str, text_changes: bool) -> None:
    chat, mid = int(rec["chat"]), int(rec["mid"])
    if text_changes:
        await bot.edit_message_text(chat_id=chat, message_id=mid, text=html2, reply_markup=scan.markup_from(kb2),
                                    link_preview_options=_NO_PREVIEW)
    else:
        await bot.edit_message_reply_markup(chat_id=chat, message_id=mid, reply_markup=scan.markup_from(kb2))


async def heal_one(bot, services, rec: dict, *, in_action: bool, pace: _Pace | None = None) -> str:
    """Поправить одно сообщение по записи. Итог: "ok" (поправлено или уже было),
    "gone" (сообщения нет), "final" (править нечего и не будет), "later" (сеть —
    повтор позже), "flood" (429 — сколько ждать, в _retry[(чат, номер)]; в
    действии повтор уже назначен фоновой задачей)."""
    from awgbot.bot.handlers.common import call
    db = services.db
    chat, mid = int(rec["chat"]), int(rec["mid"])
    async with _lock(chat):
        # запись перечитывается под замком чата: две правки разом (вытеснение из двух
        # действий) — вторая увидит уже поправленное и ничего не пошлёт
        fresh = [r for r in await call(db.list_msg_due, None, chat) if int(r["mid"]) == mid]
        if not fresh:
            return "ok"
        rec = fresh[0]
        html2, kb2, text_changes = _plan(rec)
        dead = [str(k) for k in (rec.get("dead") or [])]
        if not text_changes and not rec.get("hide"):
            # ссылки мёртвые, а текста нет (запись без html): снять ключи, править нечего
            await call(db.list_msg_put, chat, mid, str(rec.get("sent") or ""), html2, None,
                       [k for k in scan.keys(html2) if k not in dead], None)
            return "ok"
        if pace is not None:
            await pace.wait()
        await _chat_pace(chat)
        token = OWN.set(True)
        try:
            await _edit(bot, rec, html2, kb2, text_changes)
        except TelegramRetryAfter as e:
            wait = min(int(getattr(e, "retry_after", 0) or 0) or WAIT_429_S, RETRY_MAX_S if in_action else WAIT_429_S)
            log.info("lists: сообщение %d чата %d — правка отложена (429, %d с), повтор %s", mid, chat, wait,
                     "фоновой задачей" if in_action else "тактом")
            if in_action:
                asyncio.get_running_loop().create_task(_retry_later(bot, services, chat, mid, wait))
            _retry[(chat, mid)] = wait
            return "flood"
        except TelegramBadRequest as e:
            why = str(e)
            if "message is not modified" in why:
                pass
            elif "message to edit not found" in why:
                keys = await call(db.list_msg_forget, chat, mid, True)
                log.info("lists: сообщение %d чата %d удалено — снимки %s сняты", mid, chat, keys)
                return "gone"
            else:
                await call(db.list_msg_final, chat, mid)
                log.info("lists: сообщение %d чата %d не поправить (%s) — запись снята, снимки %s отвечают отметкой",
                         mid, chat, why, dead)
                return "final"
        except TelegramForbiddenError as e:
            await call(db.list_msg_final, chat, mid)
            log.info("lists: сообщение %d чата %d не поправить (%s) — запись снята, снимки %s отвечают отметкой",
                     mid, chat, e, dead)
            return "final"
        except (TelegramNetworkError, TelegramServerError, TelegramUnauthorizedError) as e:
            log.info("lists: сообщение %d чата %d — правка отложена (%s), повтор тактом", mid, chat, e)
            return "later"
        finally:
            OWN.reset(token)
        # запись — по новому виду (посредник при «not modified» ответа не видел)
        await call(db.list_msg_put, chat, mid, str(rec.get("sent") or ""), html2, kb2,
                   [k for k in scan.keys(html2) if k not in dead], None)
        if dead:
            log.info("lists: сообщение %d чата %d — ссылки на снимки %s сняты правкой", mid, chat, dead)
        return "ok"


async def _retry_later(bot, services, chat: int, mid: int, wait: int) -> None:
    await asyncio.sleep(wait)
    try:
        from awgbot.bot.handlers.common import call
        recs = await call(services.db.list_msg_due, None, chat)
        for rec in recs:
            if int(rec["mid"]) == mid:
                await heal_one(bot, services, rec, in_action=False)
    except Exception as e:                             # noqa: BLE001
        log.warning("lists: повтор правки сообщения %d чата %d не удался: %s", mid, chat, e)


async def heal_chat(bot, services, chat: int) -> int:
    """Правка чата в действии (привязалась новая ссылка): сообщения с мёртвыми
    ключами — сейчас, до ответа обработчику. Возвращает число поправленных."""
    from awgbot.bot.handlers.common import call
    recs = await call(services.db.list_msg_due, None, chat)
    done = 0
    for rec in recs:
        if rec.get("dead") and await heal_one(bot, services, rec, in_action=True) == "ok":
            done += 1
    return done


async def nightly(bot, services) -> dict:
    """Такт после полуночи и догон на старте: снимки с истёкшим сроком — в отметки,
    записи к правке — по одной правке на сообщение; по чатам параллельно, в чате
    не чаще одной правки в секунду, на бота — BOT_RATE в секунду. Нечего делать —
    ни одного запроса в Telegram."""
    from awgbot.bot.handlers.common import call
    expired = await call(services.db.list_snap_expire_due)
    recs = await call(services.db.list_msg_due)
    by_chat: dict[int, list[dict]] = {}
    for rec in recs:
        by_chat.setdefault(int(rec["chat"]), []).append(rec)
    stats = {"expired": len(expired), "links": 0, "hide": 0, "later": 0}
    pace = _Pace(1.0 / BOT_RATE)
    sem = asyncio.Semaphore(PARALLEL_CHATS)

    async def one_chat(items: list[dict]) -> None:
        async with sem:
            waited = False
            for i, rec in enumerate(items):
                out = await heal_one(bot, services, rec, in_action=False, pace=pace)
                if out == "flood":
                    key = (int(rec["chat"]), int(rec["mid"]))
                    if not waited:
                        # первый 429 в чате — подождать, сколько просят (не дольше WAIT_429_S), и повторить
                        waited = True
                        await asyncio.sleep(min(_retry.pop(key, WAIT_429_S), WAIT_429_S))
                        out = await heal_one(bot, services, rec, in_action=False, pace=pace)
                    if out == "flood":
                        _retry.pop(key, None)
                        stats["later"] += len(items) - i      # остаток чата — догону или следующему такту
                        return
                if out == "ok":
                    stats["links"] += 1 if rec.get("dead") else 0
                    stats["hide"] += 1 if rec.get("hide") else 0
                elif out == "later":
                    stats["later"] += 1

    await asyncio.gather(*(one_chat(items) for items in by_chat.values()))
    # строка — всегда, и когда делать было нечего: по журналу видно, что такт прошёл
    log.info("lists: такт — истекло снимков %d, поправлено ссылок %d, снято «Скрыть» %d, отложено %d",
             stats["expired"], stats["links"], stats["hide"], stats["later"])
    return stats
