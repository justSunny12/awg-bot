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


# ── восстановление бэкапа = переход на выпуск бэкапа ─────────────────────────

def restore_version_line(kind: str, cur: str, ver: str) -> str:
    """Т-В1: строка версии в плане (первой после заголовка). kind — down | up."""
    cur, ver = _e(_ver(cur)), _e(_ver(ver))
    if kind == "down":
        return (f"♻️ Версия: сейчас {cur}, бэкап снят на {ver} — сервер AWG сначала вернётся на {ver} "
                f"(выпуск с GitHub, sha256 сверю с бэкапом), затем восстановится; шлюзы перейдут на {ver} следом")
    return (f"♻️ Версия: сейчас {cur}, бэкап снят на {ver} — сервер AWG сначала обновится до {ver}, затем "
            f"восстановится; шлюзы перейдут на {ver} следом")


def restore_withdrawn_line(ver: str, reason: str) -> str:
    """Т-В1, хвост: выпуск бэкапа помечен снятым."""
    return (f"⚠️ Выпуск {_e(_ver(ver))} снят: {_e(reason)}. Восстановить можно, но после восстановления лучше "
            "обновиться кнопкой «⬆️ Обновить»")


# Т-В2: отказы осмотра (после «⚠️ Файл не принят: »)
def restore_not_release(ver: str) -> str:
    return f"бэкап снят на сборке не из выпуска ({_ver(ver)}) — восстановить его можно только на той же версии"


def restore_no_release(ver: str) -> str:
    v = _ver(ver)
    return f"выпуска {v} на GitHub нет — восстановить этот бэкап можно только на {v}, поставив её вручную"


def restore_other_digest(ver: str) -> str:
    return (f"выпуск {_ver(ver)} на GitHub не совпадает с тем, на котором снят бэкап (sha256 другой) — "
            "переходить на него не буду")


def restore_github_down(ver: str) -> str:
    return f"не удалось проверить выпуск {_ver(ver)} на GitHub — попробуй позже"


def restore_fetch_failed(ver: str) -> str:
    """После «♻️ Восстановить»: выпуск бэкапа не скачался или не сошёлся sha256."""
    return f"выпуск {_ver(ver)} с GitHub не скачался — попробуй позже"


def restore_not_launched(reason: str) -> str:
    """После «♻️ Восстановить»: выпуск бэкапа не получен — ничего не запущено,
    вопрос остаётся."""
    return f"⚠️ Восстановление не запущено: {_e(reason)}"


def restore_meta_bad(ver: str) -> str:
    return f"метаданные выпуска {_ver(ver)} на GitHub не прочитаны — переходить на него не буду"


def restore_version_done(ver: str) -> str:
    """Т-В3: строка в отчёте после восстановления с переходом."""
    return f"Версия: {_e(_ver(ver))} — как в бэкапе"


def restore_version_failed(ver: str, reason: str, cur: str) -> str:
    """Т-В3: отказ перехода (новым процессом, прежняя версия)."""
    return (f"⚠️ Переход на {_e(_ver(ver))} не удался: {_e(reason)} — ничего не изменилось, работает "
            f"{_e(_ver(cur))}")
