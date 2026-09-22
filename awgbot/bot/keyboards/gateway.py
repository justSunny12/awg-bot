"""Роль gateway (агент на шлюзе): панель, настройки, обслуживание, бандл."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from awgbot.core import settings
from awgbot.bot.callbacks import UpdateCB, GwCB

from .common import _chk
from .settings import _enc_label


def gateway_panel_kb() -> InlineKeyboardMarkup:
    """Панель шлюза: обновить | монитор здоровья / мастер восстановления /
    настройки. Мастер — на подтверждение: обрыв РФ у всех, пусть на секунды,
    не должен случаться от промаха пальцем. Перезапуски — в настройках."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Статус", callback_data=GwCB(action="refresh"))
    kb.button(text="🌡 Монитор здоровья", callback_data=GwCB(action="health"))
    kb.button(text="🔧 Мастер восстановления", callback_data=GwCB(action="reassert"))
    kb.button(text="⚙️ Настройки", callback_data=GwCB(action="settings"))
    kb.adjust(2, 1, 1)
    return kb.as_markup()


def gateway_settings_kb() -> InlineKeyboardMarkup:
    """Тот же порядок, что у основного бота; чего у шлюза нет (сервер,
    маршрутизация, подписки) — нет и здесь. Мониторинг и резервное
    копирование, как и там, живут в «Обслуживании»; доступ по SSH — свой
    раздел (порт как факт, фильтр снаружи)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔔 Уведомления", callback_data=GwCB(action="notify"))
    kb.button(text="✉️ E-mail", callback_data=GwCB(action="email"))
    kb.button(text="🛡 Доступ по SSH", callback_data=GwCB(action="ssh"))
    kb.button(text="🔄 Обслуживание", callback_data=GwCB(action="maint"))
    kb.button(text="⬆️ Обновления бота", callback_data=GwCB(action="updates"))
    kb.button(text="⬅️ В меню", callback_data=GwCB(action="panel"))
    kb.adjust(1)
    return kb.as_markup()


def _gw_back(sec: str = "settings") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="\u2b05\ufe0f Назад", callback_data=GwCB(action=sec).pack())


def gateway_notify_kb() -> InlineKeyboardMarkup:
    """Как у основного, без событий клиентов: CPU и RAM в одной строке, ниже —
    диск и температура."""
    s = settings
    kb = InlineKeyboardBuilder()
    ef = s.get_bool("notifications.email_fallback", False)
    kb.button(text=f"{_chk(ef)} E-mail при недоступности Telegram",
              callback_data=GwCB(action="tgl", val="notifications.email_fallback"))
    qh = s.get_bool("quiet_hours.quiet_hours_enabled", True)
    kb.button(text=f"{_chk(qh)} Тихие часы",
              callback_data=GwCB(action="tgl", val="quiet_hours.quiet_hours_enabled"))
    rows = [1, 1]
    if qh:
        kb.button(text=f"Начало: {s.get_int('quiet_hours.quiet_hours_start', 20)}:00 МСК",
                  callback_data=GwCB(action="edit", val="quiet_hours.quiet_hours_start"))
        kb.button(text=f"Конец: {s.get_int('quiet_hours.quiet_hours_end', 7)}:00 МСК",
                  callback_data=GwCB(action="edit", val="quiet_hours.quiet_hours_end"))
        rows.append(2)
    ra = s.get_bool("resource_alerts.enabled", True)
    kb.button(text=f"{_chk(ra)} Алерты хоста (CPU/RAM/диск/температура)",
              callback_data=GwCB(action="tgl", val="resource_alerts.enabled"))
    rows.append(1)
    if ra:
        kb.button(text=f"CPU: {s.get_int('resource_alerts.thresholds_percent.cpu', 80)}%",
                  callback_data=GwCB(action="edit", val="resource_alerts.thresholds_percent.cpu"))
        kb.button(text=f"RAM: {s.get_int('resource_alerts.thresholds_percent.ram', 80)}%",
                  callback_data=GwCB(action="edit", val="resource_alerts.thresholds_percent.ram"))
        kb.button(text=f"Диск: {s.get_int('resource_alerts.thresholds_percent.disk', 80)}%",
                  callback_data=GwCB(action="edit", val="resource_alerts.thresholds_percent.disk"))
        kb.button(text=f"Temp: {s.get_int('app.gateway.temp_alert_c', 75)} °C",
                  callback_data=GwCB(action="edit", val="app.gateway.temp_alert_c"))
        rows += [2, 2]
    kb.adjust(*rows)
    kb.row(_gw_back())
    return kb.as_markup()


def gateway_mon_kb() -> InlineKeyboardMarkup:
    """«Мониторинг» основного бота как есть, на ключах шлюза."""
    s = settings
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Частота опроса: {s.get_int('app.gateway.monitor_minutes', 3)} мин",
              callback_data=GwCB(action="edit", val="app.gateway.monitor_minutes"))
    kb.button(text=f"Отсчётов до сработки алерта: {s.get_int('app.monitoring.alert_streak', 5)}",
              callback_data=GwCB(action="edit", val="app.monitoring.alert_streak"))
    loud = s.get_bool("app.gateway.link_alert_loud", True)
    kb.button(text=f"{_chk(loud)} Алерт простоя линка со звуком 24/7",
              callback_data=GwCB(action="tgl", val="app.gateway.link_alert_loud"))
    kb.button(text=f"Порог простоя линка: {s.get_int('app.gateway.handshake_max_age', 300)} сек",
              callback_data=GwCB(action="edit", val="app.gateway.handshake_max_age"))
    kb.adjust(1)
    kb.row(_gw_back("maint"))
    return kb.as_markup()


def gateway_backup_kb(encryption: bool = False) -> InlineKeyboardMarkup:
    s = settings
    kb = InlineKeyboardBuilder()
    on = s.get_bool("app.scheduler.backup_enabled", True)
    kb.button(text=f"{_chk(on)} Резервное копирование",
              callback_data=GwCB(action="tgl", val="app.scheduler.backup_enabled"))
    rows = [1]
    if on:
        kb.button(text=_enc_label(encryption), callback_data=GwCB(action="enc"))
        ch = str(s.get("app.scheduler.backup_channel", "telegram") or "telegram").lower()
        for val, label in (("telegram", "Telegram"), ("email", "E-mail")):
            kb.button(text=f"{'✅' if ch == val else '☑️'} {label}",
                      callback_data=GwCB(action="bk_ch", val=val))
        kb.button(text=f"📆 День месяца для автобэкапа: {s.get_int('app.scheduler.backup_day', 1)}",
                  callback_data=GwCB(action="edit", val="app.scheduler.backup_day"))
        kb.button(text=f"🕘 Время запуска автобэкапа: {s.get_int('app.scheduler.backup_hour', 12)}:00",
                  callback_data=GwCB(action="edit", val="app.scheduler.backup_hour"))
        kb.button(text="💾 Создать резервную копию", callback_data=GwCB(action="backup!"))
        rows += [1, 2, 1, 1, 1]
    kb.adjust(*rows)
    kb.row(_gw_back("maint"))
    return kb.as_markup()


def gateway_email_kb(configured: bool) -> InlineKeyboardMarkup:
    """Почта у агента: те же кнопки, что у основного, без аварийного выхода."""
    kb = InlineKeyboardBuilder()
    if not configured:
        kb.button(text="✉️ Подключить ящик", callback_data=GwCB(action="em_setup"))
        kb.adjust(1)
    else:
        kb.button(text="🔍 Проверить соединение", callback_data=GwCB(action="em_check"))
        kb.button(text="📨 Тестовое письмо", callback_data=GwCB(action="em_test"))
        kb.button(text="✏️ Сменить ящик", callback_data=GwCB(action="em_setup"))
        kb.button(text="🗑 Отключить", callback_data=GwCB(action="em_forget"))
        kb.adjust(2, 2)
    kb.row(_gw_back())
    return kb.as_markup()


def gateway_email_forget_confirm() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data=GwCB(action="email"))
    kb.button(text="✅ Отключить", callback_data=GwCB(action="em_forget!"))
    kb.adjust(2)
    return kb.as_markup()


def gateway_email_offer(back: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=GwCB(action=back))
    kb.button(text="✉️ Настроить почту", callback_data=GwCB(action="em_setup"))
    kb.adjust(2)
    return kb.as_markup()


def gateway_encryption_kb(has_secret: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Сменить фразу" if has_secret else "🔑 Задать фразу",
              callback_data=GwCB(action="enc_set"))
    kb.button(text="\u2b05\ufe0f Назад", callback_data=GwCB(action="backup"))
    kb.adjust(1)
    return kb.as_markup()


_ALLOW_BUTTONS = 40     # Telegram: до 100 кнопок; больше сорока адресов — уже не тот инструмент


def gateway_ssh_kb(st: dict) -> InlineKeyboardMarkup:
    """Раздел «🛡 Доступ по SSH» агента — зеркало settings_firewall основного
    бота: порт, адреса (val — номер записи, не адрес: IPv6 ломал бы упаковку),
    фильтр. На обвязке старого образца кнопок фильтра нет — включать нечего."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🅿️ Изменить порт", callback_data=GwCB(action="ssh_port"))
    kb.button(text="➕ Добавить адрес", callback_data=GwCB(action="ssh_add"))
    # Весь список — кнопками: инфобокс его не показывает, только число
    for i, entry in enumerate((st.get("allow") or [])[:_ALLOW_BUTTONS]):
        kb.button(text=f"➖ {entry}", callback_data=GwCB(action="ssh_del", val=str(i)))
    if st.get("new_plumbing"):
        if st.get("filter"):
            kb.button(text="🔴 Выключить фильтр", callback_data=GwCB(action="ssh_off"))
        else:
            kb.button(text="🟢 Включить фильтр", callback_data=GwCB(action="ssh_on"))
    kb.adjust(1)
    kb.row(_gw_back("settings"))
    return kb.as_markup()


def gateway_ssh_port_finisher_kb() -> InlineKeyboardMarkup:
    """Финишер «порт не изменился / не выполнена»: другой порт или раздел;
    нажатие оставляет финишер с одной «Скрыть»."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить порт", callback_data=GwCB(action="ssh_port_retry"))
    kb.button(text="\u2b05\ufe0f Назад", callback_data=GwCB(action="ssh_port_back"))
    kb.adjust(1)
    return kb.as_markup()


def gateway_ssh_confirm_kb(action: str, val: str, label: str) -> InlineKeyboardMarkup:
    """Подтверждение действия раздела SSH (включить/выключить фильтр, убрать
    адрес) — «Отмена» первой, возврат в раздел."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data=GwCB(action="ssh"))
    kb.button(text=label, callback_data=GwCB(action=action, val=val))
    kb.adjust(2)
    return kb.as_markup()


def gateway_cancel_kb(sec: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f Отмена", callback_data=GwCB(action=sec))
    return kb.as_markup()


def gateway_maint_kb() -> InlineKeyboardMarkup:
    """Порядок тот же, что в «Обслуживании» основного бота."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Мониторинг", callback_data=GwCB(action="mon"))
    kb.button(text="💾 Резервное копирование", callback_data=GwCB(action="backup"))
    kb.button(text="🔁 Перезапустить AWG", callback_data=GwCB(action="restart"))
    kb.button(text="🔁 Перезапустить бота", callback_data=GwCB(action="botrestart"))
    kb.button(text="⬅️ Назад", callback_data=GwCB(action="settings"))
    kb.adjust(1)
    return kb.as_markup()


def gateway_updates_kb(muted: bool) -> InlineKeyboardMarkup:
    """Раздел обновлений агента — один в один с основным ботом: тумблер
    уведомлений (при расписании «никогда» принудительно выключен), пикер
    расписания с отметкой текущего, ручная проверка. Назад — в настройки."""
    sched = str(settings.get("updates.poll_schedule", "day")).lower()
    never = sched == "never"
    notify_on = (not muted) and not never
    lbl = "Уведомлять об обновлениях" + (" (выкл: расписание «никогда»)" if never else "")
    kb = InlineKeyboardBuilder()
    kb.button(text=f"{_chk(notify_on)} {lbl}", callback_data=GwCB(action="upd_toggle"))
    labels = {"day": "Каждый день", "week": "Раз в неделю",
              "month": "Раз в месяц", "never": "Никогда"}
    for opt, text in labels.items():
        mark = "🔘 " if opt == sched else ""
        kb.button(text=f"{mark}{text}", callback_data=GwCB(action="upd_sched", val=opt))
    kb.button(text="🔍 Проверить сейчас", callback_data=GwCB(action="upd_check"))
    kb.button(text="⬅️ Назад", callback_data=GwCB(action="settings"))
    kb.adjust(1, 2, 2, 1, 1)
    return kb.as_markup()


def gateway_confirm_kb(action: str, back: str = "panel") -> InlineKeyboardMarkup:
    """«Не надо» первой — необратимое не там, куда палец идёт по инерции.
    back — куда возвращает отказ: мастер живёт на панели, перезапуски — в
    обслуживании."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data=GwCB(action=back))
    kb.button(text="✅ Выполнить", callback_data=GwCB(action=f"{action}!"))
    kb.adjust(2)
    return kb.as_markup()


def gateway_bundle_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data=GwCB(action="drop"))
    kb.button(text="📦 Применить бандл", callback_data=GwCB(action="apply!"))
    kb.adjust(2)
    return kb.as_markup()


def gateway_bundle_passphrase_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Оставить свою", callback_data=GwCB(action="apply_keep!"))
    kb.button(text="🔐 Перезаписать", callback_data=GwCB(action="apply_ow!"))
    kb.adjust(2)
    return kb.as_markup()


def gateway_back_kb(sec: str = "") -> InlineKeyboardMarkup:
    """Одна кнопка назад: без sec — «В меню» (панель), с sec — «Назад» в раздел
    (отказ смены порта возвращает в «Доступ по SSH»)."""
    kb = InlineKeyboardBuilder()
    if sec:
        kb.row(_gw_back(sec))
    else:
        kb.button(text="⬅️ В меню", callback_data=GwCB(action="panel"))
    return kb.as_markup()


def gateway_update_available_kb() -> InlineKeyboardMarkup:
    """«Есть ступень» у агента: Обновить + назад в раздел. Не update_notify():
    это ответ на ручную проверку, ему «Скрыть» и «Не уведомлять» не нужны."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬆️ Обновить", callback_data=UpdateCB(action="install"))
    kb.button(text="⬅️ Назад", callback_data=GwCB(action="updates"))
    kb.adjust(1, 1)
    return kb.as_markup()
