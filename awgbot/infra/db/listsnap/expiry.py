"""expiry.py — часть пакета listsnap: сроки снимков и кнопки «Скрыть».

Срок снимка — полночь в поясе показа после 30 дней: `exp` = 00:00 даты `at`
(в поясе показа) + 31 день; живёт от 30 до 31 дня, надпись «списки событий
бот хранит 30 дней» остаётся правдой. Пишется в пояс хранения; смена пояса
показа после создания срок не двигает. Снимок v1 (без `exp`) — тот же срок,
выведенный из `at` на лету.

«Скрыть» снимается тактом после полуночи у сообщений не моложе HIDE_KEEP_S
(сутки минус полчаса — запас к окну джиттера такта): кнопка живёт около
суток и до почти двух и всегда моложе предела удаления Telegram (48 часов) —
в том числе через сутки с переходом времени (проверка «к следующему такту»).
Сравнивается время хоста со временем Telegram (`sent` — `date` ответа) —
часы хоста сверены (правило проекта)."""
from __future__ import annotations

from datetime import datetime, timedelta

from awgbot.util import timeutil

TTL_S = 30 * 86400                 # основа срока снимка: 30 дней, затем до полуночи
TOMB_TTL_S = 90 * 86400            # отметка «больше не хранится»
HIDE_KEEP_S = 24 * 3600 - 30 * 60  # «Скрыть» — не моложе 23 ч 30 мин на такте


def expires_at(at: datetime) -> datetime:
    """Момент смерти снимка, созданного в `at`: полночь пояса показа после 30 дней."""
    day = at.astimezone(timeutil.TZ).date() + timedelta(days=TTL_S // 86400 + 1)
    # полночь — стенные часы пояса показа (fold=0: в сутки с переходом — первый момент этого дня)
    return datetime(day.year, day.month, day.day, tzinfo=timeutil.TZ)


def exp_iso(at_iso: str) -> str:
    """`exp` для тела снимка — из `at` (ISO хранения)."""
    return timeutil.to_iso(expires_at(timeutil.parse_iso(at_iso)))


def due_at(data: dict) -> datetime | None:
    """Момент смерти снимка по телу: v2 — `exp`, v1 — из `at`; нечитаемое — None
    (такой снимок считается протухшим)."""
    try:
        if data.get("exp"):
            return timeutil.parse_iso(str(data["exp"]))
        return expires_at(timeutil.parse_iso(str(data.get("at") or "")))
    except ValueError:
        return None


def is_due(data: dict, now: datetime | None = None) -> bool:
    """Снимок пора сжать в отметку: срок наступил (или тело нечитаемо)."""
    end = due_at(data)
    return end is None or end <= (now or timeutil.now())


DELETE_LIMIT_S = 48 * 3600         # Telegram удаляет сообщение бота только первые 48 часов
TICK_END_S = 6 * 60                # такт после полуночи кончается к 00:06
TICK_MARGIN_S = 30 * 60            # запас к пределу удаления


def hide_due(rec: dict, now: datetime | None = None) -> bool:
    """«Скрыть» на сообщении из записи пора снять: отправлено не позже HIDE_KEEP_S назад,
    либо к следующему такту (следующая полночь пояса показа + 00:06, по абсолютному
    времени — в сутки с переходом времени они длиннее) сообщению было бы больше
    предела удаления минус запас. Нечитаемый `sent` — снять (кнопка неизвестного
    возраста в чате не стоит)."""
    try:
        sent = timeutil.parse_iso(str(rec.get("sent") or ""))
    except ValueError:
        return True
    now = now or timeutil.now()
    if timeutil.elapsed(sent, now) >= HIDE_KEEP_S:
        return True
    day = now.astimezone(timeutil.TZ).date() + timedelta(days=1)
    next_tick = datetime(day.year, day.month, day.day, tzinfo=timeutil.TZ) + timedelta(seconds=TICK_END_S)
    return timeutil.elapsed(sent, next_tick) >= DELETE_LIMIT_S - TICK_MARGIN_S
