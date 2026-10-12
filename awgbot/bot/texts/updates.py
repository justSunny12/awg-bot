"""Обновления бота (self-update): уведомления, проверка, итоги."""

from __future__ import annotations

from .fmt import _e
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


def update_available(tag: str, body: str, installed: str | None = None) -> str:
    """Уведомление о доступной новой версии — цели обновления. Тело — её
    changelog; пропущенные ступени не перечисляются: старшая версия включает
    правки младших."""
    from awgbot.core import config
    cur = _ver(installed if installed is not None else config.INSTALLED_VERSION)
    header = (f"Текущая версия бота {_e(cur)}.\n"
              f"Доступна новая версия: {_e(_ver(tag))}\n"
              "Список изменений:\n")
    return header + _changelog_block(body, header, tag)


UPDATE_NOTHING = "Обновлять не на что — версия актуальна"
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
    "updates_notify_toast", "update_wait", "update_failed"]
