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
    "email.resume_address": ("Адрес для кода", "Пришли адрес почты; «-» — сам ящик"),
    "app.network.server_host": ("Домен", "Пришли домен или IP — попадёт в новые ссылки"),
    "app.client_config.server_name": ("Имя сервера", "Пришли имя — его видно клиенту в приложении"),
    "app.client_config.dns1": ("DNS клиентов", "Пришли один или два адреса через запятую"),
    "app.firewall.ssh_allow": ("Адреса для SSH-доступа",
                               "Пришли IP, подсеть или доменное имя через пробел; имя буду резолвить сам"),
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
    """Диалог ввода: «✏️ <b>Частота опроса</b> · сейчас 3 мин» ⏎⏎ «Пришли число от
    1 до 1440 (мин)»; текстовые — со своей подсказкой."""
    if key in SETTINGS_TEXT:
        label, hint = SETTINGS_TEXT[key]
        if isinstance(current, (list, tuple)):
            shown = _setting_value(key, list(current)[:5]) + (f" и ещё {len(current) - 5}" if len(current) > 5 else "")
        else:
            shown = _setting_value(key, current) if current not in (None, "", []) else ""
        icon = "➕" if key == "app.firewall.ssh_allow" else "✏️"      # эмодзи кнопки «➕ Адрес»
        return ui.head(f"{icon} {_e(label)}", meta=[f"сейчас {shown}" if shown else ""]) + f"\n\n{_e(hint)}"
    lo, hi, label, unit = _bounds(key)
    if unit == HOUR:
        return (ui.head(f"✏️ {_e(label)}", meta=[f"сейчас {hour_value(current)}" if current is not None else ""])
                + f"\n\nПришли час от {lo} до {hi}")
    u = unit_suffix(unit)
    return (ui.head(f"✏️ {_e(label)}", meta=[f"сейчас {current}{u}" if current is not None else ""])
            + f"\n\nПришли число от {lo} до {hi}" + (f" ({unit})" if unit else ""))


def bonus_prompt(current: str) -> str:
    """Приглашение к вводу бонуса при исчерпании: три формата и выключение."""
    return (ui.head("✏️ Бонус при исчерпании", meta=[f"сейчас {_e(current)}"]) + "\n\n"
            "Сколько добавить профилю разово, когда он упёрся в месячный лимит:\n"
            "• <code>50</code> — 50 ГБ\n"
            "• <code>10%</code> — 10% лимита профиля, с округлением вверх до целых ГБ, но не больше половины лимита\n"
            "• <code>10% 50</code> — то же, но не больше 50 ГБ\n"
            "• <code>0</code> — без бонуса: доступ отключится сразу, как только лимит исчерпан, и владелец "
            "профиля может не узнать об этом: без доступа уведомление до него может не дойти")


BONUS_BAD = "⚠️ Не понял. Пришли число ГБ (<code>50</code>), процент (<code>10%</code>) или оба (<code>10% 50</code>)"


def bonus_changed(old: str, new: str) -> str:
    return ui.changed("Бонус при исчерпании", _e(old), _e(new))


HOUR = "час"           # та же метка, что в sections/notify.BOUNDS: часы показываются как «20:00»


def hour_value(v) -> str:
    """Граница тихих часов для экрана: 20 → «20:00»."""
    try:
        return f"{int(v):02d}:00"
    except (TypeError, ValueError):
        return _e(str(v))


def unit_suffix(unit: str) -> str:
    """Единица после числа: «%» вплотную, остальные через пробел; у часов
    единицы нет — значение само вида «20:00»."""
    if not unit or unit == HOUR:
        return ""
    return unit if unit == "%" else f" {unit}"


def settings_changed(key: str, old, new) -> str:
    """Итог ввода — первой строкой раздела: «✅ Частота опроса: 3 → 5 мин»."""
    if key in SETTINGS_TEXT:
        label, unit = SETTINGS_TEXT[key][0], ""
    else:
        _lo, _hi, label, unit = _bounds(key)
        if unit == HOUR:
            return ui.changed(_e(label), hour_value(old) if old is not None else "—",
                              hour_value(new) if new is not None else "—")
        unit = unit_suffix(unit)
    old_s = _setting_value(key, old) if old not in (None, "", []) else "—"
    new_s = _setting_value(key, new) if new not in (None, "", []) else "—"
    return ui.changed(_e(label), old_s, new_s, unit)


def settings_ssh_allow_added(entries: list) -> str:
    # адреса — моноширинным: жирный адрес Telegram превращает в ссылку
    return ("✅ Адреса для SSH-доступа: добавлено "
            + ", ".join(f"<code>{_e(x)}</code>" for x in entries))


def settings_bad_value(key: str) -> str:
    """Переспрос: «⚠️ Частота опроса — число от 1 до 1440 мин, попробуй ещё раз»."""
    lo, hi, label, unit = _bounds(key)
    if unit == HOUR:
        return f"⚠️ {_e(label)} — час от {lo} до {hi}, попробуй ещё раз"
    return f"⚠️ {_e(label)} — число от {lo} до {hi}{unit_suffix(unit)}, попробуй ещё раз"


# подписи тумблеров общих разделов — для всплывашки итога «Тихие часы: вкл»
_TOGGLE_LABELS = {
    "quiet_hours.quiet_hours_enabled": "Тихие часы",
    "resource_alerts.enabled": "Алерты хоста",
    "notifications.email_fallback": "Аварии на e-mail",
    "app.scheduler.backup_enabled": "Автобэкапы",
    "email.resume_enabled": "Аварийный выход",
    "app.monitoring.service_failure_alert_loud": "Простой со звуком",
    "app.gateway.link_alert_loud": "Простой со звуком",
    "notifications.client_events.activation": "Активация",
    "notifications.client_events.grace": "Отсрочка",
    "notifications.client_events.over_limit": "Лимит исчерпан",
    "notifications.client_events.bonus": "Бонус",
}


def toggle_toast(key: str, on: bool) -> str:
    """Всплывашка итога тумблера: «Тихие часы: вкл», «Аварии на e-mail: выкл»;
    ключ без подписи — «Включено» / «Выключено»."""
    label = _TOGGLE_LABELS.get(key)
    state = "вкл" if on else "выкл"
    return f"{label}: {state}" if label else ("Включено" if on else "Выключено")


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
