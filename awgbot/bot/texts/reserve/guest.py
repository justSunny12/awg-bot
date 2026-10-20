"""guest.py — тексты бота основы на резерве (гостевой режим): первая строка
главной админа (Т-РГ1), всплывашка недоступного раздела (Т-РГ2), «⚙️
Настройки» на резерве (Т-РГ3), конфигурация старого устройства (Т-РГ4),
надзор вебхука (Т-РУ19 основе)."""
from __future__ import annotations

from awgbot.util.textfmt.base import details

from ..fmt import _e
from .section import _n, _when

from awgbot.domain.services.reserve.guest import DEVICE_CONFIG_LATER, GUEST_REFUSED

UNAVAILABLE = GUEST_REFUSED                         # Т-РГ2
__all__ = ["UNAVAILABLE", "DEVICE_CONFIG_LATER", "main_line", "primary_line", "settings_text", "card_text",
           "webhook_stalled", "webhook_restored"]
SETTINGS_ABOUT = ("Бот работает на резерве {name} — настройки сервера, шлюзы, бэкапы и обновления вернутся "
                  "вместе с клиентами")

_REASONS = {
    "dead": "сервер AWG не отвечает",
    "button": "ты перевёл клиентов кнопкой",
    "rf": "сервер AWG недоступен из РФ",
    "nogw": "шлюзы не на связи с ним, РФ-доступ идёт через шлюзы {name}",
}


def main_line(d: dict) -> str:
    """Т-РГ1: «🛟 Работаю на резерве «Beta» с 03:12 — сервер AWG не отвечает»;
    основа снова в строю — подсказка, где рычаг возврата (рычаг «по кнопке»)."""
    name = _n(d.get("name") or "резерв")
    since = _when(d.get("since") or "")
    head = f"🛟 Работаю на резерве {name}" + (f" с {since}" if since else "")
    primary = d.get("primary") or {}
    if primary.get("status") == "alive" and d.get("return") == "button" and d.get("reason") != "nogw":
        return head + " · сервер AWG снова в строю — «⚙️ Настройки» → «🛟 Резерв» → «↩️ Вернуть клиентов»"
    reason = _REASONS.get(str(d.get("reason") or ""), "")
    return head + (f" — {reason.format(name=name)}" if reason else "")


def primary_line(d: dict) -> str:
    """Строка сервера основы в Т-РГ3: по состоянию, которое знает резерв."""
    own = _n(d.get("own_name") or "сервер")
    name = _n(d.get("name") or "резерв")
    primary = d.get("primary") or {}
    st, since = str(primary.get("status") or ""), _when(primary.get("since") or "")
    tail = f" с {since}" if since else ""
    if st == "alive":
        if d.get("reason") == "nogw":
            return f"сервер AWG {own}: 🟢 на связи · шлюзы не на связи с {name}{tail} — РФ-доступ через шлюзы {name}"
        return f"сервер AWG {own}: 🟢 на связи"
    if st == "rf":
        return f"сервер AWG {own}: 🚫 недоступен из РФ{tail} — {name} его видит, шлюзы нет"
    if st == "dead":
        return f"сервер AWG {own}: 🔴 не отвечает{tail}"
    return f"сервер AWG {own}: состояние неизвестно"


def settings_text(d: dict) -> str:
    """Т-РГ3: «⚙️ Настройки» на резерве — одна карточка резерва."""
    from awgbot.bot import ui
    name = _n(d.get("name") or "резерв")
    since = _when(d.get("since") or "")
    card = f"🛟 <b>Резерв {name}</b> 🟢 несёт нагрузку" + (f" с {since}" if since else "")
    return "\n\n".join([ui.head("⚙️ Настройки"), SETTINGS_ABOUT.format(name=name),
                        card + "\n" + details(primary_line(d))])


def card_text(d: dict) -> str:
    """Карточка «🛟 Резерв» на резерве: та же, что в Т-РГ3, плюс число устройств на связи."""
    name = _n(d.get("name") or "резерв")
    since = _when(d.get("since") or "")
    lines = [f"🛟 <b>Резерв {name}</b> 🟢 несёт нагрузку" + (f" с {since}" if since else "")]
    rows = [primary_line(d)]
    online = int(d.get("online") or 0)
    if online:
        rows.append(f"на связи здесь: {online}")
    lines.append(details("\n".join(rows)))
    return "\n".join(lines)


def webhook_stalled(reserve_name: str, since: str, error: str) -> str:
    """Т-РУ19 основе (в её чат на резерве): Telegram не доставляет обновления."""
    return (f"⚠️ Telegram не доставляет обновления твоего бота на {_n(reserve_name)} с {_when(since)}: "
            f"<code>{_e(error)}</code> — ответы задерживаются, Telegram хранит сообщения сутки; админ "
            f"{_n(reserve_name)} знает")


def webhook_restored(reserve_name: str) -> str:
    return f"🟢 Доставка на {_n(reserve_name)} восстановлена"
