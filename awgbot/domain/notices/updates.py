"""updates.py — часть пакета notices (см. __init__): итог self-update после
рестарта и список изменений под катом (его же рисует экран обновлений бота)."""

from __future__ import annotations

from awgbot.util.textfmt import _e


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


ADMIN_REISSUE_TAG = "v3.3.0"     # выпуск, с которого конфигам админа есть что получить заново


def admin_reissue_advice() -> str:
    """Разово на старте после обновления: конфиги устройств админа, выданные
    раньше, не знают ни резолвера админа (имена SMB в туннеле), ни — при
    раздельном туннеле — пространства адресов в туннеле."""
    url = release_url(ADMIN_REISSUE_TAG)
    return ("🔁 Перевыпусти конфигурации своих устройств — так на них заработают SMB в туннеле и адреса "
            f'локальных сетей в туннеле (<a href="{_e(url)}">что нового в {ADMIN_REISSUE_TAG}</a>)')


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
    # строки «#requires_…» и «#awg_genN» — адресация обновления, человеку не нужна
    body = "\n".join(ln for ln in (body or "").splitlines() if not ln.lstrip().startswith("#")).strip()
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


def _ver(v: str) -> str:
    """Версия с буквой v: голое «2.4.2.8» Telegram принимает за IP-адрес и
    рисует ссылкой. Теги релизов уже с буквой — не удваиваем."""
    v = str(v or "").strip()
    return v if v.startswith("v") else f"v{v}"


def update_applied(tag: str, body: str) -> str:
    """Итог успешного self-update (после рестарта): остаётся в истории.
    Changelog установленной версии — под катом, как в уведомлении."""
    header = f"✅ Обновлено до {_e(_ver(tag))}\n"
    return header + _changelog_block(body, header, tag)


def update_not_applied(tag: str, installed: str) -> str:
    return (f"⚠️ Обновление до {_e(_ver(tag))} не применилось, работает "
            f"{_e(_ver(installed))} — журнал: <code>journalctl -u awg-bot-selfupdate*</code>")
