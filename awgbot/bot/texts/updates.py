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
    """Уведомление о доступной новой версии — цели набора обновлений. Тело — её
    changelog; пропущенные ступени не перечисляются: старшая версия включает
    правки младших. installed — от чего («сейчас»): выпуск набора."""
    from awgbot.core import config
    cur = _ver(installed if installed is not None else config.INSTALLED_VERSION)
    header = f"⬆️ <b>Доступна {_e(_ver(tag))}</b> · сейчас {_e(cur)}\n"
    return header + _changelog_block(body, header, tag)


def _seen_date(seen: str) -> str:
    from awgbot.util import timeutil
    try:
        return timeutil.fmt_date_ui(timeutil.parse_iso(seen)) if seen else ""
    except ValueError:
        return ""


_NAMES_SHOWN = 3            # пропущенных релизов в строках раздела


UPDATE_NOTHING = "Текущая версия актуальна"
UPDATE_TARGET_CHANGED_TOAST = "Цель обновления изменилась — открой «⬆️ Обновления» заново"
UPDATE_CHECK_FAILED_TOAST = "Не удалось проверить релизы на GitHub — попробуй позже"
UPDATE_STARTING = "Запускаю обновление…"
UPDATES_MUTED_TOAST = "Уведомления об обновлениях выключены"


def update_blocked_toast(reason: str) -> str:
    """Ожидание другой стороны — не окончательный отказ: «Пока нельзя: …»."""
    return f"Пока нельзя: {reason}"


def updates_notify_toast(*, enabled: bool) -> str:
    return "Уведомления об обновлениях " + ("включены" if enabled else "выключены")


__all__ = [
    "_TG_LIMIT", "CHANGELOG_URL", "release_url", "changelog_link", "_changelog_block", "_ver",
    "update_applied", "update_not_applied", "changelog_details", "update_available",
    "UPDATE_NOTHING", "UPDATE_STARTING", "UPDATES_MUTED_TOAST", "update_blocked_toast",
    "updates_notify_toast", "UPDATE_TARGET_CHANGED_TOAST", "UPDATE_CHECK_FAILED_TOAST",
    "update_skipped_lines", "_seen_date"]


def update_skipped_lines(skipped) -> list[str]:
    """Релизы, которые не рассматриваются: метаданные не прочитаны (О8)."""
    return [f"⚠️ {_e(_ver(tag))} пропущен: метаданные релиза не прочитаны — "
            "попробую снова при следующей проверке обновлений" for tag, _why in list(skipped or [])[:_NAMES_SHOWN]]
