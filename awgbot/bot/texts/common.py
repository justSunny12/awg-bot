"""Общее: статус awg-сервиса, отмена текстового диалога, перезапуск бота,
финишер устаревшего вопроса."""

from __future__ import annotations

import html as _html
import re as _re

TEXT_MAX = 4096                                     # предел текста сообщения Telegram (видимый)
_TAG = _re.compile(r"<[^>]+>")


def stale_finisher(head: str, original: str) -> str:
    """Финишер устаревшего вопроса: исходный текст сообщения без изменений, но
    под катом («подробнее»), над ним жирным — почему не актуально. Вложенный
    кат Telegram не принимает — внутренний снимается; не влезает в предел —
    исходный текст под катом без разметки и с отрезанным хвостом."""
    inner = original.replace("<blockquote expandable>", "").replace("<blockquote>", "").replace("</blockquote>", "")
    out = f"<b>{_html.escape(head, quote=False)}</b>\n\n<blockquote expandable>{inner}</blockquote>"
    if len(_html.unescape(_TAG.sub("", out))) <= TEXT_MAX:
        return out
    room = TEXT_MAX - len(head) - 4
    flat = _html.unescape(_TAG.sub("", inner))[:max(room - 1, 0)] + "…"
    return f"<b>{_html.escape(head, quote=False)}</b>\n\n<blockquote expandable>{_html.escape(flat, quote=False)}</blockquote>"




# ── Статус awg-сервиса (монитор) ─────────────────────────────────────────────
# Без слова «контейнер»: после переезда на хост его нет, и алерт отправлял бы
# чинить не тот слой (docs/ROADMAP.md, шаг 2).
HB_SERVER_DOWN = "🔴 Сервис AWG недоступен — <code>awg</code> не отвечает"
HB_SERVER_UP = "🟢 Сервис AWG снова в строю"


# ── Отмена текстового диалога: что именно отменено ──────────────────────────
# Ключ — группа состояний FSM (часть до «:» в state.get_state()). Голое
# «Отменено.» не говорило, какой из диалогов прерван, а их у админа десяток.
_CANCELLED = {
    "AddDevice": "Добавление устройства отменено",
    "AddDeviceGuide": "Добавление устройства отменено",
    "AdminSelfAddDevice": "Добавление устройства отменено",
    "AdminAddDevice": "Добавление устройства профилю отменено",
    "CreateClient": "Создание профиля отменено",
    "EditName": "Переименование профиля отменено",
    "EditDeviceName": "Переименование устройства отменено",
    "PauseDays": "Пауза подписки отменена",
    "EditPeriod": "Изменение периода подписки отменено",
    "EditLimit": "Изменение лимита устройств отменено",
    "EditTrafficLimit": "Изменение лимита трафика отменено",
    "BlockPauseDays": "Блокировка профиля отменена",
    "SettingsInput": "Изменение настройки отменено",
    "GatewayToken": "Ввод токена шлюза отменён",
    "GatewayBotToken": "Смена токена бота шлюза отменена",
    "GatewayJoinCode": "Ввод кода с экрана шлюза отменён",
    "MigrationPort": "Подготовка переезда отменена",
    "RoutingDomains": "Добавление адресов отменено",
    "Broadcast": "Объявление отменено",
    "EmailSetup": "Настройка почты отменена",
    "BackupPassphrase": "Настройка шифрования бэкапов отменена",
}


def cancelled(state: str | None) -> str:
    """Финишер под reply-«Отмена»: «<что> отменено» по группе состояний;
    вне диалога — просто «Отменено»."""
    group = (state or "").split(":", 1)[0]
    return _CANCELLED.get(group, "Отменено")


# Остаётся в чате: единственный след того, что перезапуск состоялся, а не завис.
BOT_RESTARTED = "✅ Бот успешно перезапущен"
