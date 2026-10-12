"""inputs.py — часть пакета settings (см. __init__): границы и подписи ключей, приглашения к вводу и итоги, всплывашки циклов."""

from __future__ import annotations

from awgbot.bot import ui
from ..fmt import _e
from .service import UPDATE_SCHEDULE_LABELS


# границы ввода ключей ролевых разделов: dotted-ключ → (мин, макс, подпись,
# единица). Ключи общих разделов — в BOUNDS их модулей (bot/sections); единая
# точка чтения — sections.bounds(key).
SETTINGS_BOUNDS = {
    "pause.pause_max_total_days": (1, 365, "Дней паузы за год", "дн."),
    "pause.monthly_pause_days": (0, 31, "Дней паузы за месяц", "дн."),
    "grace.grace_days": (1, 365, "Отсрочка", "дн."),
    # MTU — в новые ссылки. 1280 — минимум IPv6, 1500 — Ethernet без запаса на
    # заголовки туннеля; выше него пакеты начинают фрагментироваться.
    "app.client_config.mtu": (1280, 1500, "MTU", ""),
}


def _bounds(key: str):
    """(мин, макс, подпись, единица) ключа — модули общих разделов, затем
    таблица выше. Импорт при вызове: sections читает тексты."""
    from awgbot.bot import sections
    return sections.bounds(key)


# Текстовые настройки (не числа): ключ → (подпись, подсказка). Ввод проверяется
# валидатором в обработчике.
SETTINGS_TEXT = {
    "email.resume_address": ("Адрес для кода", "адрес почты; «-» — сам ящик"),
    "app.network.server_host": ("Домен", "домен или IP — попадёт в новые ссылки"),
    "app.client_config.server_name": ("Имя сервера", "видно клиенту в приложении"),
    "app.client_config.dns1": ("DNS клиентов", "один или два адреса через запятую"),
    "app.firewall.ssh_allow": ("Адреса для SSH-доступа", "IP, подсеть или имя DynDNS; можно несколько"),
}


# Настройки-адреса: значения на экране — моноширинным (тап копирует, автоссылки нет).
_ADDRESS_KEYS = {"app.network.server_host", "app.client_config.dns1", "app.firewall.ssh_allow",
                 "email.resume_address"}


def _setting_value(key: str, value) -> str:
    """Значение настройки для экрана: адреса — каждый в <code>, остальное — текстом.
    Списки и «a, b» через запятую — поэлементно."""
    items = list(value) if isinstance(value, (list, tuple)) else [x.strip() for x in str(value).split(",")]
    items = [str(x) for x in items if str(x).strip()]
    if key in _ADDRESS_KEYS:
        return ", ".join(f"<code>{_e(x)}</code>" for x in items)
    return _e(", ".join(items))


def settings_prompt(key: str, current=None) -> str:
    """«✏️ Частота опроса · сейчас 3 мин · 1–1440» — приглашение к вводу с
    текущим значением и границами; текстовые — с подсказкой."""
    if key in SETTINGS_TEXT:
        label, hint = SETTINGS_TEXT[key]
        if isinstance(current, (list, tuple)):
            shown = _setting_value(key, list(current)[:5]) + (f" и ещё {len(current) - 5}" if len(current) > 5 else "")
        else:
            shown = _setting_value(key, current) if current not in (None, "", []) else ""
        icon = "➕" if key == "app.firewall.ssh_allow" else "✏️"      # эмодзи кнопки «➕ Адрес»
        return ui.head(f"{icon} {_e(label)}", meta=[f"сейчас {shown}" if shown else ""]) + f"\n{_e(hint)}"
    lo, hi, label, unit = _bounds(key)
    u = unit_suffix(unit)
    return ui.head(f"✏️ {_e(label)}", meta=[f"сейчас {current}{u}" if current is not None else "", f"{lo}–{hi}"])


def bonus_prompt(current: str) -> str:
    """Приглашение к вводу бонуса при исчерпании: три формата и выключение."""
    return (ui.head("✏️ Бонус при исчерпании", meta=[f"сейчас {_e(current)}"]) + "\n"
            "Сколько добавить профилю разово, когда он упёрся в месячный лимит:\n"
            "• <code>50</code> — 50 ГБ\n"
            "• <code>10%</code> — 10% лимита профиля, окр. вверх до целых ГБ (но не более половины лимита)\n"
            "• <code>10% 50</code> — 10% лимита, вверх до целых ГБ, но не более 50 ГБ\n"
            "• <code>0</code> — без бонуса: доступ отключится сразу по достижении лимита, и уведомление "
            "об этом хозяин профиля может не получить")


BONUS_BAD = "⚠️ Не понял — жду целое число, число со знаком процента либо их корректную комбинацию"


def bonus_changed(old: str, new: str) -> str:
    return ui.changed("Бонус при исчерпании", _e(old), _e(new))


def unit_suffix(unit: str) -> str:
    """Единица после числа: «%» вплотную, остальные через пробел."""
    if not unit:
        return ""
    return unit if unit == "%" else f" {unit}"


def settings_changed(key: str, old, new) -> str:
    """Итог ввода — первой строкой раздела: «✅ Частота опроса: 3 → 5 мин»."""
    if key in SETTINGS_TEXT:
        label, unit = SETTINGS_TEXT[key][0], ""
    else:
        _lo, _hi, label, unit = _bounds(key)
        unit = unit_suffix(unit)
    old_s = _setting_value(key, old) if old not in (None, "", []) else "—"
    new_s = _setting_value(key, new) if new not in (None, "", []) else "—"
    return ui.changed(_e(label), old_s, new_s, unit)


def settings_ssh_allow_added(entries: list) -> str:
    # адреса — моноширинным: жирный адрес Telegram превращает в ссылку
    return ("✅ Адреса для SSH-доступа: добавлено "
            + ", ".join(f"<code>{_e(x)}</code>" for x in entries))


def settings_bad_value(key: str) -> str:
    lo, hi, _label, unit = _bounds(key)
    return f"⚠️ Нужно целое число {lo}–{hi}{unit_suffix(unit)}"


def cycle_toast(key: str, value) -> str:
    """Всплывашка после кнопки-цикла: «Опрос: 5 мин», «Код: 12 символов»,
    «Проверка: неделя», «Куда: E-mail»."""
    if key == "email.poll_interval_sec":
        return f"Опрос: {int(value) // 60} мин"
    if key == "email.resume_code_len":
        return f"Код: {value} символов"
    if key == "updates.poll_schedule":
        return f"Проверка: {UPDATE_SCHEDULE_LABELS.get(str(value), value)}"
    if key == "app.scheduler.backup_channel":
        return "Куда: " + ("E-mail" if str(value) == "email" else "Telegram")
    if key == "app.routing.probe_seconds":
        return f"Такт: {value} с"
    if key == "app.routing.failover.window_samples":
        return f"Окно: {value}"
    if key == "app.routing.failover.min_availability":
        return f"Порог: {value}%"
    if key == "app.routing.lists_refresh_hours":
        return f"Списки: раз в {value} ч"
    return "Готово"
