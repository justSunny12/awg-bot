"""Обновления бота (self-update): уведомления, проверка, итоги."""

from __future__ import annotations

from .fmt import _e


# ── Обновления бота (self-update) ────────────────────────────────────────────
# Лимит сообщения Telegram — 4096 символов. Changelog кладём в сворачиваемую
# цитату; если тело релиза + шапка не влезают, режем тело по границе строки.
_TG_LIMIT = 4096
CHANGELOG_URL = "https://github.com/justSunny12/awg-bot/blob/main/docs/CHANGELOG.md"


def release_url(tag: str) -> str:
    """Страница релиза на GitHub — список изменений версии. Админ читает
    релиз, а не коммиты и диффы."""
    from awgbot.core import config
    repo = getattr(config, "UPDATES_REPO", "") or "justSunny12/awg-bot"
    return f"https://github.com/{repo}/releases/tag/{tag}"


def changelog_link(tag: str = "") -> str:
    """Хвост обрезанного списка изменений — ссылка на страницу релиза; без
    тега — на журнал целиком."""
    url = release_url(tag) if tag else CHANGELOG_URL
    return f'<a href="{_e(url)}">Весь список изменений — на GitHub</a>'


def _changelog_block(body: str, header: str, tag: str = "") -> str:
    """<blockquote expandable> с телом релиза, усечённым под лимит Telegram.

    Тело экранируем целиком ДО обрезки (рвать нечего — тегов внутри нет), режем
    по границам строк под остаток бюджета. Обрезали — честный хвост. Возвращает
    готовую цитату (или пустую строку, если тела нет)."""
    body = (body or "").strip()
    if not body:
        return ""
    tail = "\n…\n" + changelog_link(tag)
    # бюджет под содержимое цитаты = лимит − шапка − теги − запас на хвост
    budget = _TG_LIMIT - len(header) - len("<blockquote expandable></blockquote>") \
        - len(tail) - 16
    esc = _e(body)
    if len(esc) <= budget:
        inner = esc
    else:
        # режем исходный текст по строкам, затем экранируем срез
        kept, used = [], 0
        for line in body.split("\n"):
            add = len(_e(line)) + 1
            if used + add > budget:
                break
            kept.append(line)
            used += add
        inner = _e("\n".join(kept)) + tail
    return f"<blockquote expandable>{inner}</blockquote>"


def changelog_details(body: str, header: str = "", header_len: int = 200, tag: str = "") -> str:
    """Список изменений под «подробнее» — для экрана раздела обновлений;
    обрезка по лимиту Telegram с хвостом-ссылкой на страницу релиза tag.
    header — настоящая шапка экрана (бюджет считается от неё), иначе — запас
    header_len."""
    return _changelog_block(body, header or " " * header_len, tag)


def _ver(v: str) -> str:
    """Версия с буквой v: голое «2.4.2.8» Telegram принимает за IP-адрес и
    рисует ссылкой. Теги релизов уже с буквой — не удваиваем."""
    v = str(v or "").strip()
    return v if v.startswith("v") else f"v{v}"


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


def update_wait(tag: str) -> str:
    """Единственное сообщение на время обновления (цепочка до него стёрта)."""
    return f"⏳ Обновление до {_e(tag)}, дождись завершения"


def update_failed(reason: str) -> str:
    return f"⚠️ Не удалось обновить: {_e(reason)}"


def update_applied(tag: str, body: str) -> str:
    """Итог успешного self-update (после рестарта): остаётся в истории.
    Changelog установленной версии — под катом, как в уведомлении."""
    header = f"✅ Обновлено до {_e(_ver(tag))}\n"
    return header + _changelog_block(body, header, tag)


def update_not_applied(tag: str, installed: str) -> str:
    return (f"⚠️ Обновление до {_e(tag)} не применилось — версия осталась "
            f"{_e(installed)}. Смотри журнал: journalctl -u awg-bot-selfupdate*")
