"""Обновления бота (self-update): уведомления, проверка, итоги."""

from __future__ import annotations

from .fmt import _e, deep_link
# итог self-update и список изменений — тексты уведомления домена, общие с экранами
from awgbot.domain.notices.updates import (  # noqa: F401
    _TG_LIMIT, CHANGELOG_URL, release_url, changelog_link, _changelog_block, _ver,
    update_applied, update_not_applied)


def changelog_details(body: str, header: str = "", header_len: int = 200, tag: str = "") -> str:
    """Список изменений под «подробнее» — для экрана раздела обновлений;
    обрезка по лимиту Telegram с хвостом-ссылкой на страницу релиза tag.
    header — настоящая шапка экрана (бюджет считается от неё), иначе — запас
    header_len."""
    return _changelog_block(body, header or " " * header_len, tag)


def update_available(tag: str, body: str, installed: str | None = None,
                     silent: dict | None = None, bot_username: str = "") -> str:
    """Уведомление о доступной новой версии — цели обновления. Тело — её
    changelog; пропущенные ступени не перечисляются: старшая версия включает
    правки младших. silent — молчащие шлюзы, чью версию цель уже не
    обслуживает (update_silent_peers): хвостом предупреждения."""
    from awgbot.core import config
    cur = _ver(installed if installed is not None else config.INSTALLED_VERSION)
    header = (f"Текущая версия бота {_e(cur)}.\n"
              f"Доступна новая версия: {_e(_ver(tag))}\n"
              "Список изменений:\n")
    tail = "\n".join(update_silent_lines(silent, bot_username)) if silent else ""
    if not tail:
        return header + _changelog_block(body, header, tag)
    tail = "\n\n" + tail
    return header + _changelog_block(body, header + tail, tag) + tail     # бюджет — с хвостом


# ── сверка версий ролей: ожидание и молчащие ─────────────────────────────────
# Данные — из домена (update_waiting, update_silent_peers): кто держит, его
# версия, нужная версия. Имя шлюза без подписи, версия в скобках; имя — ссылка
# на карточку слота (gw-<слот>).

_NAMES_SHOWN = 3            # шлюзов в строке ожидания; дальше — «и ещё N»


def _peer_ver(p) -> str:
    return _ver(p.version) if p.version else "версия неизвестна"


def update_gw_names(peers, bot_username: str = "", *, links: bool = True) -> str:
    """«Pi5 (v3.2.1), Pi4 (v3.2.0) и ещё 2» — шлюзы, которые держат или молчат;
    links=False — простым текстом (всплывашка)."""
    items = [(deep_link(bot_username, f"gw-{p.slot}", p.name) if links else p.name)
             + f" ({_peer_ver(p)})" for p in peers]
    out = ", ".join(items[:_NAMES_SHOWN])
    if len(items) > _NAMES_SHOWN:
        out += f" и ещё {len(items) - _NAMES_SHOWN}"
    return out


def update_wait_line(w: dict, bot_username: str = "") -> str:
    """Строка ожидания раздела «⬆️ Обновления»: основной бот — какие шлюзы
    держат (О2), агент — какой сервер нужен (О3)."""
    tag, need = _e(_ver(w["tag"])), _e(_ver(w["need"]))
    if w.get("role") == "gateway":
        server = w["peers"][0].version if w.get("peers") else ""
        if not server:
            return f"⏳ {tag} ждёт сервер AWG {need}: версия сервера неизвестна — он не на связи"
        now = "" if server == "old" else f" (сейчас {_e(_ver(server))})"
        return f"⏳ {tag} ждёт сервер AWG {need}{now} — сначала обнови бота сервера AWG"
    peers = w.get("peers") or []
    todo = ("обнови их агентов в чатах ботов шлюзов" if len(peers) > 1
            else "обнови агента в чате бота шлюза")
    return f"⏳ {tag} ждёт шлюзы: {update_gw_names(peers, bot_username)} — {todo}"


def update_wait_reason(w: dict) -> str:
    """Почему кнопка не ставит: условие закрылось между проверкой и нажатием
    (О5, после «Нельзя: »). Простой текст — для всплывашки."""
    tag = _ver(w["tag"])
    if w.get("role") == "gateway":
        return f"{tag} ждёт сервер AWG {_ver(w['need'])}"
    return f"{tag} ждёт шлюзы: {update_gw_names(w.get('peers') or [], links=False)}"


def _seen_date(seen: str) -> str:
    from awgbot.util import timeutil
    try:
        return timeutil.fmt_date_ui(timeutil.parse_iso(seen)) if seen else ""
    except ValueError:
        return ""


def update_silent_lines(s: dict | None, bot_username: str = "") -> list[str]:
    """Предупреждения о молчащих шлюзах (О4): цель их версию агента уже не
    обслуживает — обновиться можно, обновить агента — когда выйдет на связь."""
    if not s:
        return []
    tag, need = _e(_ver(s["tag"])), _e(_ver(s["need"]))
    out = []
    for p in s.get("peers") or []:
        name = deep_link(bot_username, f"gw-{p.slot}", p.name)
        date = _seen_date(p.seen)
        since = f" с {date}" if date else ""
        if p.version:
            what = f"({_e(_ver(p.version))}): {tag} эту версию агента не обслуживает"
        else:
            what = f"(версия агента неизвестна): для {tag} нужен агент не ниже {need}"
        out.append(f"⚠️ {name} не на связи{since} {what} — когда шлюз выйдет на связь, обнови его агента")
    return out


UPDATE_NOTHING = "Обновлять не на что — версия актуальна"
UPDATE_TARGET_CHANGED_TOAST = "Цель обновления изменилась — открой «⬆️ Обновления» заново"
UPDATE_CHECK_FAILED_TOAST = "Не удалось проверить релизы на GitHub — попробуй позже"
UPDATE_STARTING = "Запускаю обновление…"
UPDATES_MUTED_TOAST = "Уведомления об обновлениях выключены"


def update_blocked_toast(reason: str) -> str:
    return f"Нельзя: {reason}"


def updates_notify_toast(*, enabled: bool) -> str:
    return "Уведомления " + ("включены" if enabled else "выключены")


def update_wait(tag: str) -> str:
    """Единственное сообщение на время обновления (цепочка до него стёрта)."""
    return f"⏳ Обновление до {_e(tag)}, дождись завершения"


def update_failed(reason: str) -> str:
    return f"⚠️ Не удалось обновить: {_e(reason)}"


__all__ = [
    "_TG_LIMIT", "CHANGELOG_URL", "release_url", "changelog_link", "_changelog_block", "_ver",
    "update_applied", "update_not_applied", "changelog_details", "update_available",
    "UPDATE_NOTHING", "UPDATE_STARTING", "UPDATES_MUTED_TOAST", "update_blocked_toast",
    "updates_notify_toast", "update_wait", "update_failed",
    "update_gw_names", "update_wait_line", "update_wait_reason", "update_silent_lines",
    "UPDATE_TARGET_CHANGED_TOAST", "UPDATE_CHECK_FAILED_TOAST", "update_hold_line", "update_skipped_lines"]


def update_hold_line(h: dict) -> str:
    """Окно ожидания после старта (О9): раздел и всплывашка кнопки. Основной
    бот — каких шлюзов ждёт, агент — сервер AWG; «до» — предел окна."""
    from awgbot.util import timeutil
    until = h["until"].astimezone(timeutil.TZ).strftime("%H:%M")
    if h.get("role") == "gateway":
        return f"⏳ Жду связи с сервером AWG после перезапуска — до {until}"
    names = list(h.get("waiting") or [])
    who = ", ".join(_e(n) for n in names[:_NAMES_SHOWN])
    if len(names) > _NAMES_SHOWN:
        who += f" и ещё {len(names) - _NAMES_SHOWN}"
    return f"⏳ Проверяю шлюзы после перезапуска: жду {who} — до {until}"


def update_skipped_lines(skipped) -> list[str]:
    """Релизы, которые не рассматриваются: метаданные не прочитаны (О8)."""
    return [f"⚠️ {_e(_ver(tag))} пропущен: метаданные релиза не прочитаны — "
            "проверю ещё раз при следующей проверке" for tag, _why in list(skipped or [])[:_NAMES_SHOWN]]
