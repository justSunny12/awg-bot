"""Обновления бота (self-update): уведомления, проверка, итоги."""

from __future__ import annotations

from .fmt import _e


# ── Обновления бота (self-update) ────────────────────────────────────────────
# Лимит сообщения Telegram — 4096 символов. Changelog кладём в сворачиваемую
# цитату; если тело релиза + шапка не влезают, режем тело по границе строки.
_TG_LIMIT = 4096
CHANGELOG_URL = "https://github.com/justSunny12/awg-bot/blob/main/docs/CHANGELOG.md"
CHANGELOG_LINK = f'<a href="{CHANGELOG_URL}">Весь список изменений — на GitHub</a>'


def _changelog_block(body: str, header: str) -> str:
    """<blockquote expandable> с телом релиза, усечённым под лимит Telegram.

    Тело экранируем целиком ДО обрезки (рвать нечего — тегов внутри нет), режем
    по границам строк под остаток бюджета. Обрезали — честный хвост. Возвращает
    готовую цитату (или пустую строку, если тела нет)."""
    body = (body or "").strip()
    if not body:
        return ""
    tail = "\n…\n" + CHANGELOG_LINK
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


def changelog_details(body: str, header: str = "", header_len: int = 200) -> str:
    """Список изменений под «подробнее» — для экрана раздела обновлений;
    обрезка по лимиту Telegram с хвостом-ссылкой на журнал. header — настоящая
    шапка экрана (бюджет считается от неё), иначе — запас header_len."""
    return _changelog_block(body, header or " " * header_len)


def _ver(v: str) -> str:
    """Версия с буквой v: голое «2.4.2.8» Telegram принимает за IP-адрес и
    рисует ссылкой. Теги релизов уже с буквой — не удваиваем."""
    v = str(v or "").strip()
    return v if v.startswith("v") else f"v{v}"


def _skipped_block(tag: str, skipped) -> str:
    """Пропущенные ступени между установленной и целью: по строке на релиз —
    тег и заголовок ссылкой на его страницу (changelog) на GitHub. Ссылки на
    diff кода нет намеренно: админ читает changelog, а не исходники. Пусто —
    ступеней нет. Строк не больше десятка: длинный хвост сворачивается."""
    from awgbot.core import config
    skipped = list(skipped or ())
    if not skipped:
        return ""
    repo = f"https://github.com/{config.UPDATES_REPO}"
    shown = skipped[-10:]
    lines = []
    for r in shown:
        title = (r.title or "").strip()
        title = title if len(title) <= 60 else title[:59] + "…"
        label = _ver(r.tag) + (f" — {title}" if title else "")
        lines.append(f"• <a href=\"{_e(f'{repo}/releases/tag/{r.tag}')}\">{_e(label)}</a>")
    if len(skipped) > len(shown):
        lines.insert(0, f"• … ещё {len(skipped) - len(shown)}")
    return "Вместе с ней встанут пропущенные версии:\n" + "\n".join(lines) + "\n"


def update_available(tag: str, body: str, installed: str | None = None,
                     skipped=()) -> str:
    """Уведомление о доступной новой версии — цели обновления. Тело — её
    changelog; пропущенные ступени между ней и установленной — списком."""
    from awgbot.core import config
    cur = _ver(installed if installed is not None else config.INSTALLED_VERSION)
    header = (f"Текущая версия бота {_e(cur)}.\n"
              f"Доступна новая версия: {_e(_ver(tag))}\n"
              + _skipped_block(tag, skipped)
              + "Список изменений:\n")
    return header + _changelog_block(body, header)


def update_current_ok(installed: str) -> str:
    """Админ-проверка: обновляться не на что."""
    return f"Текущая версия бота ({_e(_ver(installed))}) актуальна"


def update_admin_available(installed: str, tag: str, body: str, skipped=()) -> str:
    """Админ-проверка: доступно обновление до цели (с пропущенными ступенями)."""
    header = (f"Текущая версия бота {_e(_ver(installed))}.\n"
              f"Доступно обновление до {_e(_ver(tag))}\n"
              + _skipped_block(tag, skipped)
              + "Список изменений:\n")
    return header + _changelog_block(body, header)


def update_blocked(tag: str, reason: str) -> str:
    return (f"⛔️ Обновление до {_e(_ver(tag))} сейчас недоступно: {_e(reason)}")


def update_wait(tag: str) -> str:
    """Единственное сообщение на время обновления (цепочка до него стёрта)."""
    return f"⏳ Обновление до {_e(tag)}, дождись завершения."


def update_failed(reason: str) -> str:
    return f"⚠️ Не удалось обновить: {_e(reason)}"


def update_applied(tag: str, body: str) -> str:
    """Итог успешного self-update (после рестарта): остаётся в истории.
    Changelog установленной версии — под катом, как в уведомлении."""
    header = f"✅ Обновлено до {_e(_ver(tag))}\n"
    return header + _changelog_block(body, header)


def update_not_applied(tag: str, installed: str) -> str:
    return (f"⚠️ Обновление до {_e(tag)} не применилось — версия осталась "
            f"{_e(installed)}. Смотри журнал: journalctl -u awg-bot-selfupdate*")
