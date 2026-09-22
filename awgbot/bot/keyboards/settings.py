"""Экран «⚙️ Настройки» (админ): разделы, почта, резервные копии, обслуживание, обновления, переезд."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from awgbot.core import settings
from awgbot.bot.callbacks import Menu, UpdateCB, SetCB, GwCB, HideCB

from .common import _chk, _tick


# ─────────────────────────────────────────────────────────────────────────────
# Экран «⚙️ Настройки» (админ). Значения читаются из settings в момент рендера —
# после правки экран перерисовывается и показывает актуальное.
# ─────────────────────────────────────────────────────────────────────────────


def settings_root() -> InlineKeyboardMarkup:
    """Порядок — от того, что трогают при настройке сервера, к тому, что
    трогают раз в полгода. Сверху три раздела про сам сервер (сеть, доступ,
    маршрутизация), потом про людей и уведомления, внизу обслуживание и
    обновления.

    Мониторинг и резервное копирование живут в «Обслуживании»: корень распух до
    десяти строк, а это как раз то, что открывают не ради настройки, а когда
    что-то чинят.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="🔔 Уведомления", callback_data=SetCB(sec="notify"))
    kb.button(text="🖥 Сервер AWG", callback_data=SetCB(sec="srv"))
    kb.button(text="🛡 Доступ по SSH", callback_data=SetCB(sec="fw"))
    # Раздел показываем ВСЕГДА: пока обвязка не развёрнута, он и есть место,
    # где её разворачивают. Прежде кнопка появлялась только после правки
    # app.yaml руками — то есть ровно после того, как человек уже сделал всё
    # сам в SSH.
    kb.button(text="🇷🇺 Условная маршрутизация", callback_data=SetCB(sec="rt"))
    kb.button(text="✉️ E-mail", callback_data=SetCB(sec="email"))
    kb.button(text="💳 Параметры подписок", callback_data=SetCB(sec="subs"))
    kb.button(text="🔄 Обслуживание", callback_data=SetCB(sec="svc"))
    kb.button(text="⬆️ Обновления бота", callback_data=SetCB(sec="upd"))
    kb.button(text="\u2b05\ufe0f В меню", callback_data=Menu(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def settings_back(sec_to: str = "root") -> InlineKeyboardMarkup:
    """Одна кнопка «Назад» — для экранов-отбивок внутри настроек."""
    kb = InlineKeyboardBuilder()
    kb.row(_back(sec_to))
    return kb.as_markup()


def settings_server(blocked: str = "", private_dns_offer: bool = False) -> InlineKeyboardMarkup:
    """Раздел «Сервер»: то, что уезжает в НОВЫЕ ссылки. Порт, подсеть и версия
    ядра только показываются: их смена — это перевыпуск профилей всем, и живёт
    она в переезде, а не в кнопке. private_dns_offer — DNS публичный, есть
    что предложить."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Доменное имя", callback_data=SetCB(sec="srv", act="edit", key="app.network.server_host"))
    kb.button(text="✏️ Имя сервера", callback_data=SetCB(sec="srv", act="edit", key="app.client_config.server_name"))
    kb.button(text="✏️ DNS клиентов", callback_data=SetCB(sec="srv", act="edit", key="app.client_config.dns1"))
    kb.button(text="✏️ MTU", callback_data=SetCB(sec="srv", act="edit", key="app.client_config.mtu"))
    if private_dns_offer:
        kb.button(text="🔒 Свой DNS-резолвер", callback_data=SetCB(sec="dns", act="open"))
    if not blocked:
        # Порт и подсеть правятся не здесь, а переездом: они вморожены в каждую
        # выданную ссылку. Кнопка ведёт на экран, который называет цену.
        kb.button(text="🚚 Сменить порт или подсеть",
                  callback_data=SetCB(sec="mig_prep", act="open"))
    kb.adjust(1)
    kb.row(_back())
    return kb.as_markup()


def private_dns_choices(migration_blocked: bool = False) -> InlineKeyboardMarkup:
    """Три решения; «сейчас» ведёт в подготовку переезда — пока переезд
    возможен."""
    kb = InlineKeyboardBuilder()
    if not migration_blocked:
        kb.button(text="🚚 Переехать сейчас", callback_data=SetCB(sec="dns", act="do", key="now"))
    kb.button(text="⏳ При следующем переезде", callback_data=SetCB(sec="dns", act="do", key="later"))
    kb.button(text="Не нужно", callback_data=SetCB(sec="dns", act="do", key="never"))
    kb.button(text="⬅️ Назад", callback_data=SetCB(sec="srv", act="open"))
    kb.adjust(1)
    return kb.as_markup()


def private_dns_offer_kb() -> InlineKeyboardMarkup:
    """Инфобокс при старте: те же три решения, «Назад» не нужен."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🚚 Переехать сейчас", callback_data=SetCB(sec="dns", act="do", key="now"))
    kb.button(text="⏳ При следующем переезде", callback_data=SetCB(sec="dns", act="do", key="later"))
    kb.button(text="Не нужно", callback_data=SetCB(sec="dns", act="do", key="never"))
    kb.adjust(1)
    return kb.as_markup()


def migration_prepare_confirm(want_port: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🚚 Поднять второй интерфейс",
              callback_data=SetCB(sec="mig_prep", act="do", key="go",
                                  val=str(want_port or "")))
    kb.button(text="✏️ Изменить порт", callback_data=SetCB(sec="mig_prep", act="edit", key="port"))
    kb.button(text="✖️ Отмена", callback_data=SetCB(sec="srv", act="open"))
    kb.adjust(1)
    return kb.as_markup()


def migration_generation_pending() -> InlineKeyboardMarkup:
    """Кнопка на сообщении «ядро нового поколения ждёт переезда»."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🚚 Подготовить переезд",
              callback_data=SetCB(sec="mig_prep", act="do", key="go"))
    kb.button(text="Скрыть", callback_data=HideCB())
    kb.adjust(1)
    return kb.as_markup()


def settings_firewall(st: dict) -> InlineKeyboardMarkup:
    """Раздел «Доступ по SSH». «Включить фильтр» — только когда есть хоть один
    адрес: фильтр без адресов открывает SSH всем и ничего не фильтрует."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🅿️ Изменить порт", callback_data=SetCB(sec="fw", act="edit", key="port"))
    kb.button(text="➕ Добавить адрес", callback_data=SetCB(sec="fw", act="edit", key="app.firewall.ssh_allow"))
    # В callback_data уезжает НОМЕР записи, а не сам адрес: разделитель полей —
    # двоеточие, и любой IPv6 («2001:db8::1») ломал упаковку с ValueError. Адрес
    # при этом уже записан в конфиг, то есть раздел переставал открываться
    # навсегда, и убрать запись из чата было нечем.
    for i, entry in enumerate(st.get("raw_allow", [])[:40]):     # весь список — кнопками
        kb.button(text=f"➖ {entry}", callback_data=SetCB(sec="fw", act="do", key="del", val=str(i)))
    if st.get("enabled"):
        kb.button(text="🔴 Выключить фильтр", callback_data=SetCB(sec="fw", act="do", key="off"))
    elif st.get("raw_allow"):
        kb.button(text="🟢 Включить фильтр", callback_data=SetCB(sec="fw", act="do", key="on"))
    kb.adjust(1)
    kb.row(_back())
    return kb.as_markup()


def ssh_port_finisher() -> InlineKeyboardMarkup:
    """Финишер «порт не изменился / не выполнена»: попробовать другой порт
    или вернуться в раздел; нажатие оставляет финишер с одной «Скрыть»."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить порт", callback_data=SetCB(sec="fw", act="do", key="port_retry"))
    kb.button(text="\u2b05\ufe0f Назад", callback_data=SetCB(sec="fw", act="do", key="port_back"))
    kb.adjust(1)
    return kb.as_markup()


def _back(sec_to: str = "root") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="\u2b05\ufe0f Назад", callback_data=SetCB(sec=sec_to).pack())


def settings_notify() -> InlineKeyboardMarkup:
    """Сверху — запасной канал (когда Telegram недоступен, это главное), затем
    тихие часы и алерты хоста; события профилей — в подменю."""
    s = settings
    kb = InlineKeyboardBuilder()
    ef = s.get_bool("notifications.email_fallback", False)
    kb.button(text=f"{_chk(ef)} E-mail при недоступности Telegram",
              callback_data=SetCB(sec="notify", act="toggle", key="notifications.email_fallback"))
    rows = [1]
    qh = s.get_bool("quiet_hours.quiet_hours_enabled", True)
    kb.button(text=f"{_chk(qh)} Тихие часы",
              callback_data=SetCB(sec="notify", act="toggle", key="quiet_hours.quiet_hours_enabled"))
    rows.append(1)
    if qh:
        kb.button(text=f"Начало: {s.get_int('quiet_hours.quiet_hours_start', 20)}:00 МСК",
                  callback_data=SetCB(sec="notify", act="edit", key="quiet_hours.quiet_hours_start"))
        kb.button(text=f"Конец: {s.get_int('quiet_hours.quiet_hours_end', 7)}:00 МСК",
                  callback_data=SetCB(sec="notify", act="edit", key="quiet_hours.quiet_hours_end"))
        rows.append(2)
    ra = s.get_bool("resource_alerts.enabled", True)
    kb.button(text=f"{_chk(ra)} Алерты хоста (CPU/RAM/диск)",
              callback_data=SetCB(sec="notify", act="toggle", key="resource_alerts.enabled"))
    rows.append(1)
    if ra:
        kb.button(text=f"CPU: {s.get_int('resource_alerts.thresholds_percent.cpu', 80)}%",
                  callback_data=SetCB(sec="notify", act="edit", key="resource_alerts.thresholds_percent.cpu"))
        kb.button(text=f"RAM: {s.get_int('resource_alerts.thresholds_percent.ram', 80)}%",
                  callback_data=SetCB(sec="notify", act="edit", key="resource_alerts.thresholds_percent.ram"))
        kb.button(text=f"Диск: {s.get_int('resource_alerts.thresholds_percent.disk', 80)}%",
                  callback_data=SetCB(sec="notify", act="edit", key="resource_alerts.thresholds_percent.disk"))
        rows.append(3)
    kb.button(text="👥 События профилей", callback_data=SetCB(sec="ncl", act="open"))
    rows.append(1)
    kb.adjust(*rows)
    kb.row(_back())
    return kb.as_markup()


CLIENT_EVENT_LABELS = (("activation", "Активация профиля"), ("grace", "Активация грейс-периода"),
                       ("over_limit", "Превышение лимита потребления"), ("bonus", "Выдача бонусного объёма"))


def settings_notify_clients() -> InlineKeyboardMarkup:
    s = settings
    kb = InlineKeyboardBuilder()
    for key, label in CLIENT_EVENT_LABELS:
        on = s.get_bool(f"notifications.client_events.{key}", True)
        kb.button(text=f"{_tick(on)} {label}",
                  callback_data=SetCB(sec="ncl", act="toggle",
                                      key=f"notifications.client_events.{key}"))
    kb.adjust(1)
    kb.row(_back("notify"))
    return kb.as_markup()


def settings_email(configured: bool) -> InlineKeyboardMarkup:
    """Почтовый канал: подключение/проверка/отключение и функции на нём."""
    s = settings
    kb = InlineKeyboardBuilder()
    rows: list[int] = []
    if not configured:
        kb.button(text="✉️ Подключить ящик", callback_data=SetCB(sec="email", act="do", key="setup"))
        rows.append(1)
    else:
        kb.button(text="🔍 Проверить соединение", callback_data=SetCB(sec="email", act="do", key="check"))
        kb.button(text="📨 Тестовое письмо", callback_data=SetCB(sec="email", act="do", key="test"))
        kb.button(text="✏️ Сменить ящик", callback_data=SetCB(sec="email", act="do", key="setup"))
        kb.button(text="🗑 Отключить", callback_data=SetCB(sec="email", act="do", key="forget"))
        rows += [2, 2]
        on = s.get_bool("email.resume_enabled", True)
        kb.button(text=f"{_chk(on)} Аварийный выход из приостановки",
                  callback_data=SetCB(sec="email", act="toggle", key="email.resume_enabled"))
        rows.append(1)
        if on:
            kb.button(text="Адрес для писем с кодом",
                      callback_data=SetCB(sec="email", act="edit", key="email.resume_address"))
            kb.button(text=f"Интервал опроса: {max(60, s.get_int('email.poll_interval_sec', 60))} сек",
                      callback_data=SetCB(sec="email", act="edit", key="email.poll_interval_sec"))
            kb.button(text=f"Длина кода: {s.get_int('email.resume_code_len', 8)}",
                      callback_data=SetCB(sec="email", act="edit", key="email.resume_code_len"))
            rows += [1, 2]
    kb.adjust(*rows)
    kb.row(_back())
    return kb.as_markup()


def email_forget_confirm() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data=SetCB(sec="email"))
    kb.button(text="✅ Отключить", callback_data=SetCB(sec="email", act="do", key="forget!"))
    kb.adjust(2)
    return kb.as_markup()


def settings_subs() -> InlineKeyboardMarkup:
    s = settings
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Бонус-квота: {s.get_int('limits.traffic_bonus_gb', 100)} ГБ",
              callback_data=SetCB(sec="subs", act="edit", key="limits.traffic_bonus_gb"))
    kb.button(text=f"Дней паузы (год): {s.get_int('pause.pause_max_total_days', 28)}",
              callback_data=SetCB(sec="subs", act="edit", key="pause.pause_max_total_days"))
    kb.button(text=f"Дней паузы (месяц): {s.get_int('pause.monthly_pause_days', 2)}",
              callback_data=SetCB(sec="subs", act="edit", key="pause.monthly_pause_days"))
    kb.button(text=f"Продолжительность грейс-периода: {s.get_int('grace.grace_days', 14)}",
              callback_data=SetCB(sec="subs", act="edit", key="grace.grace_days"))
    kb.adjust(1)
    kb.row(_back())
    return kb.as_markup()


def settings_mon() -> InlineKeyboardMarkup:
    s = settings
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Частота опроса: {s.get_int('app.scheduler.monitor_minutes', 3)} мин",
              callback_data=SetCB(sec="mon", act="edit", key="app.scheduler.monitor_minutes"))
    kb.button(text=f"Отсчётов до сработки алерта: {s.get_int('app.monitoring.alert_streak', 5)}",
              callback_data=SetCB(sec="mon", act="edit", key="app.monitoring.alert_streak"))
    loud = s.get_bool("app.monitoring.service_failure_alert_loud", True)
    kb.button(text=f"{_chk(loud)} Алерт простоя AWG со звуком 24/7",
              callback_data=SetCB(sec="mon", act="toggle", key="app.monitoring.service_failure_alert_loud"))
    kb.button(text=f"Порог простоя: {s.get_int('app.monitoring.service_failure_alert_minutes', 5)} мин",
              callback_data=SetCB(sec="mon", act="edit", key="app.monitoring.service_failure_alert_minutes"))
    kb.adjust(1)
    kb.row(_back("svc"))
    return kb.as_markup()


def _enc_label(enabled: bool) -> str:
    return "🔐 Шифрование: " + ("✅ включено" if enabled else "🔴 выключено")


def settings_backup(encryption: bool = False) -> InlineKeyboardMarkup:
    """Рубильник первым; выключен — остальных кнопок нет, как в условной
    маршрутизации: настраивать выключенное — приглашение к недоумению.
    «Шифрование» — сразу под рубильником."""
    s = settings
    kb = InlineKeyboardBuilder()
    on = s.get_bool("app.scheduler.backup_enabled", True)
    kb.button(text=f"{_chk(on)} Резервное копирование",
              callback_data=SetCB(sec="backup", act="toggle", key="app.scheduler.backup_enabled"))
    rows = [1]
    if on:
        kb.button(text=_enc_label(encryption), callback_data=SetCB(sec="backup", act="do", key="enc"))
        rows.append(1)
        ch = str(s.get("app.scheduler.backup_channel", "telegram") or "telegram").lower()
        for val, label in (("telegram", "Telegram"), ("email", "E-mail")):
            kb.button(text=f"{'✅' if ch == val else '☑️'} {label}",
                      callback_data=SetCB(sec="backup", act="pick", key="channel", val=val))
        kb.button(text=f"📆 День месяца для автобэкапа: {s.get_int('app.scheduler.backup_day', 1)}",
                  callback_data=SetCB(sec="backup", act="edit", key="app.scheduler.backup_day"))
        kb.button(text=f"🕘 Время запуска автобэкапа: {s.get_int('app.scheduler.backup_hour', 12)}:00",
                  callback_data=SetCB(sec="backup", act="edit", key="app.scheduler.backup_hour"))
        kb.button(text="💾 Создать резервную копию", callback_data=SetCB(sec="backup", act="do", key="now"))
        rows += [2, 1, 1, 1]
    kb.adjust(*rows)
    kb.row(_back("svc"))
    return kb.as_markup()


def backup_encryption_kb(has_secret: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Сменить фразу" if has_secret else "🔑 Задать фразу",
              callback_data=SetCB(sec="backup", act="do", key="enc_set"))
    kb.button(text="\u2b05\ufe0f Назад", callback_data=SetCB(sec="backup"))
    kb.adjust(1)
    return kb.as_markup()


def restore_confirm(gateway: bool = False) -> InlineKeyboardMarkup:
    """«Отмена» первой: восстановление необратимо, промах пальцем не должен
    возвращать всех на неделю назад."""
    kb = InlineKeyboardBuilder()
    if gateway:
        kb.button(text="⬅️ Отмена", callback_data=GwCB(action="restore_drop"))
        kb.button(text="♻️ Восстановить", callback_data=GwCB(action="restore!"))
    else:
        kb.button(text="⬅️ Отмена", callback_data=SetCB(sec="backup", act="do", key="restore_drop"))
        kb.button(text="♻️ Восстановить", callback_data=SetCB(sec="backup", act="do", key="restore!"))
    kb.adjust(2)
    return kb.as_markup()


def email_setup_offer(back_sec: str) -> InlineKeyboardMarkup:
    """«Почта не настроена» — настроить сейчас или вернуться в раздел."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=SetCB(sec=back_sec))
    kb.button(text="✉️ Настроить почту", callback_data=SetCB(sec="email", act="do", key="setup"))
    kb.adjust(2)
    return kb.as_markup()


def settings_svc(migration: str = "", available: bool = False,
                 orphans: int = 0) -> InlineKeyboardMarkup:
    """Обслуживание. Рычаг переезда показывается, только когда он настроен
    (available) — пустые ключи в app.yaml означают, что фичи нет вовсе, и
    показывать кнопку, которой некуда нажать, незачем.

    Идёт переезд — вместо «начать» два выхода. Оба через подтверждение:
    завершение роняет непереехавших, отмена возвращает всех на старые конфиги.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Мониторинг", callback_data=SetCB(sec="mon"))
    kb.button(text="💾 Резервное копирование", callback_data=SetCB(sec="backup"))
    kb.button(text="🔄 Перезапустить AWG", callback_data=SetCB(sec="svc", act="do", key="awg"))
    kb.button(text="🔄 Перезапустить бота", callback_data=SetCB(sec="svc", act="do", key="bot"))
    if available:
        if migration:
            kb.button(text="👥 Кто не переехал",
                      callback_data=SetCB(sec="mig", act="do", key="pending"))
            kb.button(text="✅ Завершить переезд",
                      callback_data=SetCB(sec="mig", act="do", key="finish"))
            kb.button(text="↩️ Отменить переезд",
                      callback_data=SetCB(sec="mig", act="do", key="cancel"))
        else:
            kb.button(text="🚚 Начать переезд профилей",
                      callback_data=SetCB(sec="mig", act="do", key="start"))
            if orphans:
                kb.button(text=f"⚠️ Переехавшие после отмены: {orphans}",
                          callback_data=SetCB(sec="mig", act="do", key="orphans"))
    kb.adjust(1)
    kb.row(_back())
    return kb.as_markup()


_MIG_CONFIRM_LABEL = {"start": "🚚 Начать", "finish": "✅ Завершить",
                      "cancel": "↩️ Отменить"}


def svc_confirm(key: str) -> InlineKeyboardMarkup:
    """Подтверждение перезапуска AWG / бота — как у агента: «Отмена» первой,
    действие с последствиями не должно стоять там, куда палец идёт по инерции."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data=SetCB(sec="svc", act="open"))
    kb.button(text="✅ Выполнить", callback_data=SetCB(sec="svc", act="do", key=f"{key}!"))
    kb.adjust(2)
    return kb.as_markup()


def migration_confirm(key: str) -> InlineKeyboardMarkup:
    """Подтверждение входа в переезд и обоих выходов. «Не надо» первой: действие
    с последствиями не должно стоять там, куда палец идёт по инерции."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Не надо", callback_data=SetCB(sec="svc", act="open"))
    kb.button(text=_MIG_CONFIRM_LABEL[key],
              callback_data=SetCB(sec="mig", act="do", key=f"{key}!"))
    kb.adjust(2)
    return kb.as_markup()


def settings_updates(muted: bool) -> InlineKeyboardMarkup:
    """muted — из services (DB-state), не из YAML. never-расписание в UI
    дизейблит тумблер уведомлений (принудительно off)."""
    s = settings
    kb = InlineKeyboardBuilder()
    sched = str(s.get("updates.poll_schedule", "day")).lower()
    never = sched == "never"
    notify_on = (not muted) and not never
    lbl = "Уведомлять об обновлениях" + (" (выкл: расписание «никогда»)" if never else "")
    kb.button(text=f"{_chk(notify_on)} {lbl}",
              callback_data=SetCB(sec="upd", act="toggle", key="notify"))
    kb.adjust(1)
    # пикер расписания
    labels = {"day": "Каждый день", "week": "Раз в неделю",
              "month": "Раз в месяц", "never": "Никогда"}
    for opt, text in labels.items():
        mark = "🔘 " if opt == sched else ""
        kb.button(text=f"{mark}{text}", callback_data=SetCB(sec="upd", act="pick", key="sched", val=opt))
    kb.button(text="🔍 Проверить сейчас", callback_data=SetCB(sec="upd", act="do", key="check"))
    kb.adjust(1, 2, 2, 1)
    kb.row(_back())
    return kb.as_markup()


def settings_cancel(sec: str) -> InlineKeyboardMarkup:
    """Отмена ввода значения — вернуться в раздел sec без изменений."""
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f Отмена", callback_data=SetCB(sec=sec))
    return kb.as_markup()


def update_notify() -> InlineKeyboardMarkup:
    """Кнопки уведомления о новой версии: Обновить / Скрыть / Не уведомлять.
    «Скрыть» — универсальная HideCB (удаляет сообщение); «один раз на версию»
    держит notified_tag в БД, не кнопка."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬆️ Обновить", callback_data=UpdateCB(action="install"))
    kb.button(text="Скрыть", callback_data=HideCB())
    kb.button(text="🔕 Не уведомлять об обновлениях", callback_data=UpdateCB(action="mute"))
    kb.adjust(2, 1)
    return kb.as_markup()


def update_admin_available() -> InlineKeyboardMarkup:
    """Админ-проверка «Обновление бота» с доступной версией: Обновить / Назад."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬆️ Обновить", callback_data=UpdateCB(action="install"))
    kb.button(text="\u2b05\ufe0f Назад", callback_data=Menu(action="main"))
    kb.adjust(2)
    return kb.as_markup()


def migration_needed() -> InlineKeyboardMarkup:
    """Инфобокс «нужен переезд»: сразу к подтверждению старта и «Скрыть».
    Скрыть — не «отложить навсегда»: сообщение приходит при каждом старте,
    пока переезд не начат."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🚚 Начать переезд профилей",
              callback_data=SetCB(sec="mig", act="do", key="start"))
    kb.button(text="Скрыть", callback_data=HideCB())
    kb.adjust(1)
    return kb.as_markup()


def update_done_menu() -> InlineKeyboardMarkup:
    """«В меню» на итоговом сообщении self-update. Свой колбэк (upd:menu), а не
    Menu(main): стандартный обработчик РЕДАКТИРУЕТ сообщение в панель, а итог
    должен остаться в истории — кнопка лишь снимается, меню приходит новым
    сообщением."""
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f В меню", callback_data=UpdateCB(action="menu"))
    return kb.as_markup()
