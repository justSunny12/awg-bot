"""c9wait.py — ожидание правок бота на резерве по отметке С9 без рычага: плашка
карточки резерва «жду события» (по сроку — со ссылкой «не ждать?») и экран
подтверждения «🗑 Не ждать» (данные — domain reserve_c9_wait_view)."""
from __future__ import annotations

from awgbot.util.textfmt.base import deep_link

from ..fmt import _e
from .common import _date, _n

C9_PAYLOAD = "rsv-c9"             # /start rsv-c9 — экран «не ждать» (ссылка в плашке карточки резерва)


def card_line(v: dict, name: str, bot_username: str = "") -> str:
    """Плашка блока «📤» карточки: отметка стоит; срок вышел и снять можно — «(не ждать?)»
    ссылкой на экран; без имени бота ссылки нет — хвост не выводится (простой текст
    без перехода ничего не даёт)."""
    if not v:
        return ""
    tail = (f" ({deep_link(bot_username, C9_PAYLOAD, 'не ждать?')})"
            if bot_username and v.get("overdue") and not v.get("drop_why") else "")
    return f"└ ⏳ жду журнал событий с {_n(name)}{tail}"


def screen_text(v: dict, name: str) -> str:
    """Подтверждение «🗑 Не ждать»: что потеряется, что известно о резерве, когда ждать стоит."""
    n = _n(name)
    if not v:
        return f"🗑 <b>Изменений от гостевого экземпляра на {n} не жду</b>\n\nОжидание уже отключено — синхронизация не выполняется"
    since = _date(v.get("since") or "")
    if v.get("drop_why"):
        now = _e(v["drop_why"])
    elif v.get("online"):
        now = f"{n} на связи, но пока не подтвердил остановку гостевого экземпляра"
    else:
        down = _date(v.get("down_since") or "")
        now = f"{n} недоступен" + (f" с {_e(down)}" if down else "") + ". Запущен ли на нём ещё гостевой экземпляр — неизвестно"
    moved = int(v.get("moved") or 0)
    lines = [f"⚠️ <b>Не ждать изменений от {n}?</b>", "",
             f"Пока гостевой экземпляр работал на {n}, пользователи могли менять настройки: продлевать доступ, добавлять профили "
             "или устройства. Этот сервер ожидает недостающий журнал событий" + (f" с {_e(since)}" if since else "")
             + f" и применит его, как только {n} вернётся в сеть.",
             "Если сбросить ожидание, <b>эти изменения будут безвозвратно утеряны.</b> Рекомендуется в качестве "
             f"крайней меры, когда есть чёткое понимание, что {n} не вернётся на связь никогда.",
             f"{now}." + (f"\nУже синхронизировано изменений: {moved}" if moved else "")]
    return "\n".join(lines)


__all__ = ["C9_PAYLOAD", "card_line", "screen_text"]
