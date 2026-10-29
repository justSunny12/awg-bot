"""
notifier.py — рассылка Notification-намерений, которые возвращают services.

services синхронны и не шлют сообщения сами; этот async-хелпер отправляет их
через бота. Ошибки отправки (например, пользователь заблокировал бота) глотаем, чтобы
одна неудача не срывала остальную рассылку.

Flood-контроль Telegram (~30 msg/с на бота): пачки (месячный сброс, массовые
алерты) шлём с лёгким пейсингом, а 429 (RetryAfter) не глотаем как прочие
ошибки — ждём указанное время и повторяем один раз, иначе уведомление молча
терялось бы именно тогда, когда рассылка большая.

Кнопки под уведомлением: своя клавиатура уведомления (reply_markup или
готовая по имени — Notification.keyboard, KEYBOARDS), иначе — кнопка действия
по подсказке Notification.action («⏱ Продлить», «📦 Без
профиля», «💳 Подписка», ACTION_LABELS) над «Скрыть», иначе одна «Скрыть».
Нажатие разбирает reply_commands.on_note_action.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InputMediaPhoto

from awgbot.core import settings
from awgbot.util import timeutil
from awgbot.bot import keyboards as kb

log = logging.getLogger("awgbot.notifier")

_BATCH_PACING_SECONDS = 0.05         # ~20 msg/с на пачку — с запасом под лимит Telegram

# Запасной канал для КРИТИЧНЫХ алертов админу, когда Telegram не отвечает
# (сеть/блокировка): runtime/main/ вешает сюда корутину, которая шлёт текст письмом.
# Только критичные (Notification.critical) и только админу: рядовые
# уведомления и чужие адресаты по почте не ездят.
_email_fallback = None


def set_email_fallback(fn) -> None:
    global _email_fallback
    _email_fallback = fn


def _network_failure(e: Exception) -> bool:
    from aiogram.exceptions import TelegramNetworkError
    return isinstance(e, (TelegramNetworkError, asyncio.TimeoutError, OSError))


async def _fallback(critical: bool, tg_id, text: str) -> bool:
    """True — алерт всё-таки доставлен, письмом."""
    from awgbot.core import config
    if not critical or _email_fallback is None or tg_id != config.ADMIN_ID:
        return False
    try:
        await _email_fallback(text)
        log.warning("критичный алерт ушёл на почту: Telegram недоступен")
        return True
    except Exception as e2:                          # noqa: BLE001
        log.warning("запасной канал (почта) не сработал: %s", e2)
        return False


async def _confirm(on_sent) -> None:
    """Сообщить отправителю, что уведомление дошло. Монитор шлюза взводит
    «алерт показан» только отсюда: запись в базу до отправки приводила к
    одинокому отбою — беда случилась, алерт не улетел (сеть у шлюза падает
    вместе с линком), а «✅ ожил» приходил как первое и единственное слово."""
    if on_sent is None:
        return
    try:
        await asyncio.to_thread(on_sent)
    except Exception as e:                           # noqa: BLE001
        log.warning("доставка уведомления не подтверждена: %s", e)


def _silent_now(force_sound: bool) -> bool:
    """Слать ли БЕЗ звука: тихие часы включены, сейчас тихое окно и уведомление
    не помечено как всегда-громкое (force_sound)."""
    if force_sound or not settings.get_bool("quiet_hours.quiet_hours_enabled", True):
        return False
    return timeutil.in_quiet_hours(settings.get_int("quiet_hours.quiet_hours_start", 20), settings.get_int("quiet_hours.quiet_hours_end", 7))


async def _send(bot, tg_id, text, markup, silent, critical: bool = False, on_sent=None):
    """Одна отправка: RetryAfter → подождать и повторить один раз; прочие
    ошибки — залогировать и продолжить рассылку. Сетевой отказ на критичном
    алерте админу — запасной канал (почта). Возвращает отправленное
    сообщение либо None; on_sent зовём при доставке любым из каналов.
    Ссылкам на экраны и списки в тексте — метка «-n» (bot/links): из
    уведомления экран открывается новым сообщением внизу, а не на месте меню выше."""
    from awgbot.bot import links
    text = links.mark_new(text)
    try:
        sent = await bot.send_message(tg_id, text, reply_markup=markup,
                                      disable_notification=silent)
        await _confirm(on_sent)
        return sent
    except TelegramRetryAfter as e:
        log.warning("flood-контроль: жду %s с и повторяю для %s", e.retry_after, tg_id)
        await asyncio.sleep(e.retry_after)
        try:
            sent = await bot.send_message(tg_id, text, reply_markup=markup,
                                          disable_notification=silent)
            await _confirm(on_sent)
            return sent
        except Exception as e2:                      # noqa: BLE001
            log.warning("Не удалось отправить уведомление %s (после retry): %s", tg_id, e2)
            if _network_failure(e2) and await _fallback(critical, tg_id, text):
                await _confirm(on_sent)
    except Exception as e:                           # noqa: BLE001
        log.warning("Не удалось отправить уведомление %s: %s", tg_id, e)
        if _network_failure(e) and await _fallback(critical, tg_id, text):
            await _confirm(on_sent)


_PARALLEL = 6                          # окно одновременно летящих отправок


class _Pace:
    """Общий темп пачки: старты отправок — не чаще одного за interval на все
    слоты окна разом. Пауза на каждом слоте отдельно давала окно × темп:
    при шести слотах — до 120 сообщений в секунду вместо заявленных 20."""

    def __init__(self, interval: float):
        self.interval = interval
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self.interval <= 0:
            return
        async with self._lock:
            now = asyncio.get_running_loop().time()
            at = max(now, self._next)
            self._next = at + self.interval
        if at > now:
            await asyncio.sleep(at - now)


async def _paced(items, fn) -> list:
    """Параллельно с окном _PARALLEL и общим темпом _BATCH_PACING_SECONDS:
    лимит Telegram ограничивает ТЕМП, а не число одновременных запросов —
    медленный ответ одного адресата не держит остальных."""
    sem = asyncio.Semaphore(_PARALLEL)
    pace = _Pace(_BATCH_PACING_SECONDS)

    async def one(item):
        async with sem:
            await pace.wait()
            return await fn(item)
    return await asyncio.gather(*(one(i) for i in items), return_exceptions=True)


# готовые клавиатуры по подсказке Notification.keyboard — домен называет
# клавиатуру по имени, собирает её бот
def _peer_code_offer():
    from awgbot.bot.keyboards import reserve as rkb
    return rkb.peer_code_offer()


def _quorum_tie():
    from awgbot.bot.keyboards import reserve as rkb
    return rkb.quorum_tie()


KEYBOARDS = {"update_done": kb.update_done_menu, "peer_code": _peer_code_offer, "quorum_tie": _quorum_tie}


def note_markup(n):
    """Своя клавиатура уведомления: reply_markup, иначе готовая по имени
    (Notification.keyboard), иначе None. Незнакомое имя — None: дальше решает
    вызывающий («Скрыть» по умолчанию)."""
    markup = getattr(n, "reply_markup", None)
    if markup is not None:
        return markup
    build = KEYBOARDS.get(getattr(n, "keyboard", "") or "")
    return build() if build is not None else None


# подписи кнопок действия по подсказке Notification.action
ACTION_LABELS = {"extend": "⏱ Продлить", "unassigned": "📦 Без профиля", "sub": "💳 Подписка",
                 "rsv_record_back": "↩️ Вернуть запись", "gw_retry": "🔁 Повторить"}


def action_markup(action, base=None):
    """Кнопка действия над «Скрыть» по подсказке уведомления; без подсказки
    или с незнакомой — одна «Скрыть». base — своя клавиатура уведомления
    (например, отсрочка): её кнопки идут первыми, «Скрыть» остаётся одна."""
    if not action or action[0] not in ACTION_LABELS:
        return base or kb.hide_only()
    from awgbot.bot import ui
    from awgbot.bot.callbacks import HideCB, NoteCB
    hide = HideCB().pack()
    # кнопки base по одной в ряд, «Скрыть» — одна, последней
    own = [btn for row in (base.inline_keyboard if base is not None else []) for btn in row
           if btn.callback_data != hide]
    return kb.append_hide_row(ui.rows(
        *own, (ACTION_LABELS[action[0]], NoteCB(kind=action[0], ref=int(action[1] if len(action) > 1 else 0)))))


async def send_notifications(bot, notifications) -> None:
    items = [n for n in (notifications or []) if n.tg_id]
    if not items:
        return
    # тихие часы — один расчёт на батч, а не три чтения настроек на письмо
    quiet_silent = _silent_now(False)

    async def one(n):
        silent = False if getattr(n, "force_sound", False) else (quiet_silent or bool(getattr(n, "silent", False)))
        markup = action_markup(getattr(n, "action", ()), note_markup(n))
        await _send(bot, n.tg_id, n.text, markup, silent,
                    critical=bool(getattr(n, "critical", False)),
                    on_sent=getattr(n, "on_sent", None))
    if len(items) == 1:
        await one(items[0])
        return
    await _paced(items, one)


async def notify_one(bot, tg_id, text, *, reply_markup=None, force_sound=False, silent=False) -> None:
    """Разовое уведомление ТРЕТЬЕМУ ЛИЦУ (не инициатору действия) — с тихими
    часами и кнопкой «Скрыть» (по умолчанию, если reply_markup не передан).
    Для ответа самому инициатору на его же действие это НЕ нужно: там
    используем обычный message.answer (глушить эхо себе бессмысленно).
    Ошибку отправки глотаем, как и в пакетной рассылке. Возвращает отправленное
    сообщение (None при неудаче) — финишеру self-update нужен его id, чтобы
    снять кнопку при следующей ступени."""
    if not tg_id:
        return None
    silent = silent or _silent_now(force_sound)        # silent — без звука и вне тихих часов
    markup = reply_markup or kb.hide_only()
    return await _send(bot, tg_id, text, markup, silent)


__all__ = ["send_notifications", "notify_one"]


async def send_announcement(bot, tg_id, text: str, photos=()):
    """Объявление ОДНИМ сообщением: картинки и текст под ними.

    Текст с картинками едет ПОДПИСЬЮ к первому вложению — так Telegram и
    склеивает альбом с текстом в один пост. Отдельным сообщением было бы две
    записи в чате вместо одной.

    Кнопка «Скрыть» есть только там, где Telegram её позволяет: у альбома
    reply_markup не бывает вовсе. Одинокую картинку можно было бы снабдить
    кнопкой, но тогда одно и то же объявление выглядело бы по-разному в
    зависимости от числа вложений — а разницы этой человек не заказывал.
    """
    if not photos:
        return await bot.send_message(tg_id, text, reply_markup=kb.hide_only())
    if len(photos) == 1:
        return await bot.send_photo(tg_id, photos[0], caption=text or None)
    media = [InputMediaPhoto(media=p, caption=(text or None) if i == 0 else None)
             for i, p in enumerate(photos)]
    return await bot.send_media_group(tg_id, media=media)


async def broadcast(bot, tg_ids, text, photos=(), texts_by_tg=None) -> tuple[int, list[int]]:
    """Массовая рассылка объявления по списку tg_id. Возвращает (доставлено,
    tg_id недоставленных — отчёт называет их поимённо). Ошибки отправки
    (заблокировали бота, удалён аккаунт) считаем в «не удалось» и продолжаем. Пейсинг между сообщениями — как в общей рассылке
    (флуд-контроль Telegram); RetryAfter внутри _send пережидается один раз.
    parse_mode берётся дефолтный (бот сконфигурирован с HTML). Тихие часы к
    объявлениям НЕ применяем — это осознанная явная отправка админом."""
    async def one(tg_id) -> bool:
        # texts_by_tg — свой текст адресату (объявление с продлением: шапка с
        # его датами); нет в карте — общий
        t = texts_by_tg.get(tg_id, text) if texts_by_tg else text
        try:
            await send_announcement(bot, tg_id, t, photos)
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await send_announcement(bot, tg_id, t, photos)
                return True
            except Exception as e2:                  # noqa: BLE001
                log.warning("broadcast: не доставлено %s (после retry): %s", tg_id, e2)
                return False
        except Exception as e:                       # noqa: BLE001
            log.warning("broadcast: не доставлено %s: %s", tg_id, e)
            return False
    ids = [t for t in tg_ids if t]
    results = await _paced(ids, one)
    ok = sum(1 for r in results if r is True)
    return ok, [t for t, r in zip(ids, results) if r is not True]
