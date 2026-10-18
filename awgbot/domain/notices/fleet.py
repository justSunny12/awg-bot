"""fleet.py — часть пакета notices (см. __init__): уведомления набора
обновлений — догон и следование шлюзов, откат сервера сторожем (чат бота
сервера AWG) и строки агента о команде сервера (чат бота шлюза).

Без звука все: о ходе набора говорит живое сообщение, эти — после него или
вне хода. Без точки в конце; команды — моноширинным.
"""
from __future__ import annotations

from awgbot.util.textfmt import _e

from .backup import chat_ref, gw_ref
from .updates import _ver

JOURNAL = "<code>journalctl -u awg-bot -o short-iso</code>"

# повторы догона: через сколько — словами (в тексте) и секундами (такт)
RETRY_WORDS = ("30 минут", "2 часа", "6 часов")


def gw_caught_up(name: str, label: str, tag: str, *, slot: int = 0, bot_username: str = "") -> str:
    """Т-У7: шлюз вышел на связь и догнал выпуск набора."""
    return (f"✅ {gw_ref(name, label, slot=slot, bot_username=bot_username)} вышел на связь и обновлён "
            f"до {_e(_ver(tag))}")


def gw_followed_down(name: str, label: str, tag: str, *, slot: int = 0, bot_username: str = "") -> str:
    """Т-У9 после понижения: агент новее выпуска набора вернулся к нему."""
    return (f"✅ {gw_ref(name, label, slot=slot, bot_username=bot_username)} вернулся к "
            f"{_e(_ver(tag))} вслед за сервером AWG")


def gw_catchup_failed(name: str, label: str, tag: str, reason: str, tries: int, *,
                      slot: int = 0, bot_username: str = "") -> str:
    """Т-У7б: догон не удался — когда повтор."""
    when = RETRY_WORDS[min(max(tries, 1), len(RETRY_WORDS)) - 1]
    return (f"⚠️ {gw_ref(name, label, slot=slot, bot_username=bot_username)} вышел на связь, но не обновился "
            f"до {_e(_ver(tag))}: {_e(reason)} — повторю через {when}")


def server_rolled_back(tag: str, prev: str) -> str:
    """Т-У8: сторож вернул прежнюю версию сервера AWG."""
    return (f"⚠️ Обновление до {_e(_ver(tag))} отменено: обновлённый сервер AWG не вышел на связь за "
            f"10 минут — вернул {_e(_ver(prev))}. Журнал: {JOURNAL}")


SERVER_SILENT = "обновлённый сервер AWG не вышел на связь за 10 минут"
AGENT_SILENT = "обновленный агент не вышел на связь за 10 минут"


# ── агент шлюза ──────────────────────────────────────────────────────────────

def agent_updating(tag: str) -> str:
    """Т-А1: строка панели, пока идёт обновление."""
    return f"⬆️ Обновляюсь до {_e(_ver(tag))} по команде сервера AWG"


def agent_updated(tag: str) -> str:
    """Т-А2: после обновления — одна строка вместо списка изменений."""
    return f"✅ Агент обновлён до {_e(_ver(tag))} по команде сервера AWG"


def agent_update_failed(tag: str, prev: str, reason: str, server_bot: dict | None = None,
                        state: str = "rolled_back") -> str:
    """Т-А3: неудача — панель до следующего удачного и уведомление. Обновлённый
    агент не вышел на связь (rolled_back) — «вернулся на»; до перезапуска не
    дошло (refused, failed) — «не выполнено, остался на»."""
    where = f"Подробности — в чате {chat_ref(server_bot, 'бота сервера AWG')}"
    if state != "rolled_back":
        return (f"⚠️ Обновление до {_e(_ver(tag))} не выполнено — агент остался на {_e(_ver(prev))}: "
                f"{_e(reason)}. {where}")
    return (f"⚠️ Обновление до {_e(_ver(tag))} не удалось — агент вернулся на {_e(_ver(prev))}: "
            f"{_e(reason)}. {where}")


def agent_reverted(prev: str, tag: str) -> str:
    """Т-А5: откат набора по команде сервера."""
    return f"↩️ Агент вернулся на {_e(_ver(prev))}: обновление набора до {_e(_ver(tag))} отменено сервером AWG"
