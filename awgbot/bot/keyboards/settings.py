"""Экран «⚙️ Настройки» (админ): разделы в два столбца, почта, бэкапы, сервис, обновления, переезд."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from awgbot.core import settings
from awgbot.bot.callbacks import Menu, UpdateCB, SetCB, GwCB, HideCB

from .common import paged_rows, _chk, _tick, entry_tag, confirm


# ─────────────────────────────────────────────────────────────────────────────
# Экран «⚙️ Настройки» (админ). Значения читаются из settings в момент рендера —
# после правки экран перерисовывается и показывает актуальное.
# ─────────────────────────────────────────────────────────────────────────────


def settings_root() -> InlineKeyboardMarkup:
    """Корень — десять кнопок в два столбца: слева то, что трогают при
    настройке сервера, справа — реже. Шлюзы живут на главной («🛰 Шлюзы»),
    в корне их нет."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔔 Уведомления", callback_data=SetCB(sec="notify"))
    kb.button(text="🖥 Сервер AWG", callback_data=SetCB(sec="srv"))
    kb.button(text="🛡 SSH-доступ", callback_data=SetCB(sec="fw"))
    kb.button(text="✉️ E-mail", callback_data=SetCB(sec="email"))
    kb.button(text="💳 Подписки", callback_data=SetCB(sec="subs"))
    kb.button(text="💾 Бэкапы", callback_data=SetCB(sec="backup"))
    kb.button(text="🩺 Мониторинг", callback_data=SetCB(sec="mon"))
    kb.button(text="🔧 Сервис", callback_data=SetCB(sec="svc"))
    kb.button(text="⬆️ Обновления", callback_data=SetCB(sec="upd"))
    kb.button(text="⬅️ В меню", callback_data=Menu(action="main"))
    kb.adjust(2)
    return kb.as_markup()


def restart_now_or_later() -> InlineKeyboardMarkup:
    """Под итогом развёртывания обвязки и подъёма интерфейса переезда: бот
    читает их при старте — перезапуск сейчас или позже (⚙️ → 🔧 Сервис)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Перезапустить сейчас", callback_data=SetCB(sec="svc", act="do", key="bot!"))
    kb.button(text="⬅️ Позже", callback_data=Menu(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def settings_back(sec_to: str = "root") -> InlineKeyboardMarkup:
    """Одна кнопка «Назад» — для экранов-отбивок внутри настроек."""
    kb = InlineKeyboardBuilder()
    kb.row(_back(sec_to))
    return kb.as_markup()


def _back(sec_to: str = "root") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="⬅️ Назад", callback_data=SetCB(sec=sec_to).pack())


def _cycle(sec: str, key: str, label: str) -> InlineKeyboardButton:
    """Кнопка-цикл: нажатие переставляет значение на следующее из ряда
    (обработчик act="cycle"), подпись — текущее значение."""
    return InlineKeyboardButton(text=label, callback_data=SetCB(sec=sec, act="cycle", key=key).pack())


# ── 🖥 Сервер AWG ────────────────────────────────────────────────────────────

def settings_server(blocked: str = "", private_dns_offer: bool = False) -> InlineKeyboardMarkup:
    """Раздел «Сервер»: то, что уезжает в НОВЫЕ ссылки. Порт и подсеть
    меняются переездом — кнопка ведёт на экран, который называет цену."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Домен", callback_data=SetCB(sec="srv", act="edit", key="app.network.server_host"))
    kb.button(text="✏️ Имя", callback_data=SetCB(sec="srv", act="edit", key="app.client_config.server_name"))
    kb.button(text="✏️ DNS", callback_data=SetCB(sec="srv", act="edit", key="app.client_config.dns1"))
    kb.button(text="✏️ MTU", callback_data=SetCB(sec="srv", act="edit", key="app.client_config.mtu"))
    rows = [2, 2]
    third = []
    if private_dns_offer:
        kb.button(text="🔒 Свой резолвер", callback_data=SetCB(sec="dns", act="open"))
        third.append(1)
    if not blocked:
        kb.button(text="🚚 Порт, подсеть", callback_data=SetCB(sec="mig_prep", act="open"))
        third.append(1)
    if third:
        rows.append(len(third))
    kb.adjust(*rows)
    kb.row(_back())
    return kb.as_markup()


def private_dns_choices(migration_blocked: bool = False) -> InlineKeyboardMarkup:
    """Три решения; «сейчас» ведёт в подготовку переезда — пока переезд
    возможен."""
    kb = InlineKeyboardBuilder()
    rows = []
    if not migration_blocked:
        kb.button(text="🚚 Переехать сейчас", callback_data=SetCB(sec="dns", act="do", key="now"))
        rows.append(1)
    kb.button(text="⏳ При переезде", callback_data=SetCB(sec="dns", act="do", key="later"))
    kb.button(text="Не нужно", callback_data=SetCB(sec="dns", act="do", key="never"))
    rows.append(2)
    kb.button(text="⬅️ Назад", callback_data=SetCB(sec="srv", act="open"))
    rows.append(1)
    kb.adjust(*rows)
    return kb.as_markup()


def private_dns_offer_kb() -> InlineKeyboardMarkup:
    """Инфобокс при старте: те же три решения, «Назад» не нужен."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🚚 Переехать сейчас", callback_data=SetCB(sec="dns", act="do", key="now"))
    kb.button(text="⏳ При переезде", callback_data=SetCB(sec="dns", act="do", key="later"))
    kb.button(text="Не нужно", callback_data=SetCB(sec="dns", act="do", key="never"))
    kb.adjust(1, 2)
    return kb.as_markup()


def migration_prepare_confirm(want_port: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🚚 Поднять интерфейс",
              callback_data=SetCB(sec="mig_prep", act="do", key="go", val=str(want_port or "")))
    kb.button(text="✏️ Свой порт", callback_data=SetCB(sec="mig_prep", act="edit", key="port"))
    kb.button(text="✖️ Отмена", callback_data=SetCB(sec="srv", act="open"))
    kb.adjust(1, 2)
    return kb.as_markup()


def migration_generation_pending() -> InlineKeyboardMarkup:
    """Кнопка на сообщении «ядро нового поколения ждёт переезда»."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🚚 Подготовить переезд",
              callback_data=SetCB(sec="mig_prep", act="do", key="go"))
    kb.button(text="Скрыть", callback_data=HideCB())
    kb.adjust(1)
    return kb.as_markup()


# ── 🛡 SSH-доступ ────────────────────────────────────────────────────────────

def settings_firewall(st: dict, page: int = 0) -> InlineKeyboardMarkup:
    """Раздел «SSH-доступ». «Фильтр снаружи» — тумблер: включить можно только
    при адресах в списке (фильтр без адресов открывает SSH всем)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🅿️ Порт", callback_data=SetCB(sec="fw", act="edit", key="port"))
    kb.button(text="➕ Адрес", callback_data=SetCB(sec="fw", act="edit", key="app.firewall.ssh_allow"))
    # В callback_data уезжает НОМЕР записи (по полному списку), а не сам адрес:
    # разделитель полей — двоеточие, и любой IPv6 ломал бы упаковку.
    allow = list(st.get("raw_allow", []) or [])
    toggle = bool(st.get("enabled")) or bool(allow)
    rows = [2, *paged_rows(kb, allow, page, static=3 if toggle else 2, screen="fw", ref=0, back=SetCB(sec="fw").pack(),
                           button=lambda i, entry: kb.button(text=f"➖ {entry}", callback_data=SetCB(sec="fw", act="do", key="del", val=f"{i}.{entry_tag(entry)}")))]
    if st.get("enabled"):
        kb.button(text="✅ Фильтр снаружи", callback_data=SetCB(sec="fw", act="do", key="off"))
        rows.append(1)
    elif allow:
        kb.button(text="☑️ Фильтр снаружи", callback_data=SetCB(sec="fw", act="do", key="on"))
        rows.append(1)
    kb.adjust(*rows)
    kb.row(_back())
    return kb.as_markup()


def ssh_port_finisher() -> InlineKeyboardMarkup:
    """Финишер «порт не изменился / не выполнена»: другой порт или назад в
    раздел; нажатие оставляет финишер с одной «Скрыть»."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Другой порт", callback_data=SetCB(sec="fw", act="do", key="port_retry"))
    kb.button(text="⬅️ Назад", callback_data=SetCB(sec="fw", act="do", key="port_back"))
    kb.adjust(2)
    return kb.as_markup()


# ── 🔔 Уведомления ───────────────────────────────────────────────────────────

def settings_notify() -> InlineKeyboardMarkup:
    """Тихие часы и их границы, алерты хоста и пороги, аварии на e-mail и
    события профилей — в две колонки, границы только при включённом."""
    s = settings
    kb = InlineKeyboardBuilder()
    rows = []
    qh = s.get_bool("quiet_hours.quiet_hours_enabled", True)
    kb.button(text=f"{_chk(qh)} Тихие часы",
              callback_data=SetCB(sec="notify", act="toggle", key="quiet_hours.quiet_hours_enabled"))
    rows.append(1)
    if qh:
        kb.button(text=f"С {s.get_int('quiet_hours.quiet_hours_start', 20):02d}:00",
                  callback_data=SetCB(sec="notify", act="edit", key="quiet_hours.quiet_hours_start"))
        kb.button(text=f"До {s.get_int('quiet_hours.quiet_hours_end', 7):02d}:00",
                  callback_data=SetCB(sec="notify", act="edit", key="quiet_hours.quiet_hours_end"))
        rows.append(2)
    ra = s.get_bool("resource_alerts.enabled", True)
    kb.button(text=f"{_chk(ra)} Алерты хоста",
              callback_data=SetCB(sec="notify", act="toggle", key="resource_alerts.enabled"))
    rows.append(1)
    if ra:
        kb.button(text=f"CPU {s.get_int('resource_alerts.thresholds_percent.cpu', 80)}%",
                  callback_data=SetCB(sec="notify", act="edit", key="resource_alerts.thresholds_percent.cpu"))
        kb.button(text=f"RAM {s.get_int('resource_alerts.thresholds_percent.ram', 80)}%",
                  callback_data=SetCB(sec="notify", act="edit", key="resource_alerts.thresholds_percent.ram"))
        kb.button(text=f"Диск {s.get_int('resource_alerts.thresholds_percent.disk', 80)}%",
                  callback_data=SetCB(sec="notify", act="edit", key="resource_alerts.thresholds_percent.disk"))
        rows.append(3)
    ef = s.get_bool("notifications.email_fallback", False)
    kb.button(text=f"{_chk(ef)} Аварии на e-mail",
              callback_data=SetCB(sec="notify", act="toggle", key="notifications.email_fallback"))
    kb.button(text="👥 События", callback_data=SetCB(sec="ncl", act="open"))
    rows.append(2)
    kb.adjust(*rows)
    kb.row(_back())
    return kb.as_markup()


CLIENT_EVENT_LABELS = (("activation", "Активация"), ("grace", "Отсрочка"),
                       ("over_limit", "Лимит исчерпан"), ("bonus", "Бонусный объём"))


def settings_notify_clients() -> InlineKeyboardMarkup:
    s = settings
    kb = InlineKeyboardBuilder()
    for key, label in CLIENT_EVENT_LABELS:
        on = s.get_bool(f"notifications.client_events.{key}", True)
        kb.button(text=f"{_tick(on)} {label}",
                  callback_data=SetCB(sec="ncl", act="toggle",
                                      key=f"notifications.client_events.{key}"))
    kb.adjust(2)
    kb.row(_back("notify"))
    return kb.as_markup()


# ── ✉️ E-mail ────────────────────────────────────────────────────────────────

EMAIL_POLL_CYCLE = (60, 300, 900)          # опрос ящика: 1 → 5 → 15 мин
EMAIL_CODE_CYCLE = (6, 8, 12)              # длина кода аварийного выхода


def email_poll_label(seconds: int) -> str:
    return f"⏱ Опрос: {max(60, int(seconds)) // 60} мин"


def email_code_label(n: int) -> str:
    n = int(n)
    word = "символа" if n in (2, 3, 4) else "символов"
    return f"🔢 Код: {n} {word}"


def settings_email(configured: bool) -> InlineKeyboardMarkup:
    """Почтовый канал: проверка и тест, смена и отключение, аварийный выход из
    паузы с адресом и циклами опроса и длины кода."""
    s = settings
    kb = InlineKeyboardBuilder()
    rows: list[int] = []
    if not configured:
        kb.button(text="✉️ Подключить ящик", callback_data=SetCB(sec="email", act="do", key="setup"))
        rows.append(1)
    else:
        kb.button(text="🔍 Проверить", callback_data=SetCB(sec="email", act="do", key="check"))
        kb.button(text="📨 Тест-письмо", callback_data=SetCB(sec="email", act="do", key="test"))
        kb.button(text="✏️ Сменить ящик", callback_data=SetCB(sec="email", act="do", key="setup"))
        kb.button(text="🗑 Отключить", callback_data=SetCB(sec="email", act="do", key="forget"))
        rows += [2, 2]
        on = s.get_bool("email.resume_enabled", True)
        kb.button(text=f"{_chk(on)} Аварийный выход",
                  callback_data=SetCB(sec="email", act="toggle", key="email.resume_enabled"))
        rows.append(1)
        if on:
            kb.button(text="✉️ Адрес для кода",
                      callback_data=SetCB(sec="email", act="edit", key="email.resume_address"))
            kb.add(_cycle("email", "email.poll_interval_sec",
                          email_poll_label(s.get_int("email.poll_interval_sec", 60))))
            kb.add(_cycle("email", "email.resume_code_len",
                          email_code_label(s.get_int("email.resume_code_len", 8))))
            rows += [2, 1]
    kb.adjust(*rows)
    kb.row(_back())
    return kb.as_markup()


def email_forget_confirm() -> InlineKeyboardMarkup:
    return confirm(SetCB(sec="email"), "🗑 Отключить", SetCB(sec="email", act="do", key="forget!"))


def email_setup_offer(back_sec: str) -> InlineKeyboardMarkup:
    """«Почта не настроена» — настроить сейчас или вернуться в раздел."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=SetCB(sec=back_sec))
    kb.button(text="✉️ Настроить почту", callback_data=SetCB(sec="email", act="do", key="setup"))
    kb.adjust(2)
    return kb.as_markup()


# ── 💳 Подписки ──────────────────────────────────────────────────────────────

def settings_subs() -> InlineKeyboardMarkup:
    s = settings
    kb = InlineKeyboardBuilder()
    kb.button(text=f"📈 Бонус: {s.get_int('limits.traffic_bonus_gb', 100)} ГБ",
              callback_data=SetCB(sec="subs", act="edit", key="limits.traffic_bonus_gb"))
    kb.button(text=f"🙏 Отсрочка: {s.get_int('grace.grace_days', 14)} дн.",
              callback_data=SetCB(sec="subs", act="edit", key="grace.grace_days"))
    kb.button(text=f"⏸️ Год: {s.get_int('pause.pause_max_total_days', 28)} дн.",
              callback_data=SetCB(sec="subs", act="edit", key="pause.pause_max_total_days"))
    kb.button(text=f"⏸️ Месяц: {s.get_int('pause.monthly_pause_days', 2)} дн.",
              callback_data=SetCB(sec="subs", act="edit", key="pause.monthly_pause_days"))
    kb.adjust(2)
    kb.row(_back())
    return kb.as_markup()


# ── 🩺 Мониторинг ────────────────────────────────────────────────────────────

def settings_mon() -> InlineKeyboardMarkup:
    s = settings
    kb = InlineKeyboardBuilder()
    kb.button(text=f"⏱ Опрос: {s.get_int('app.scheduler.monitor_minutes', 3)} мин",
              callback_data=SetCB(sec="mon", act="edit", key="app.scheduler.monitor_minutes"))
    kb.button(text=f"🔢 Замеров: {s.get_int('app.monitoring.alert_streak', 5)}",
              callback_data=SetCB(sec="mon", act="edit", key="app.monitoring.alert_streak"))
    kb.button(text=f"⏳ Простой: {s.get_int('app.monitoring.service_failure_alert_minutes', 5)} мин",
              callback_data=SetCB(sec="mon", act="edit", key="app.monitoring.service_failure_alert_minutes"))
    loud = s.get_bool("app.monitoring.service_failure_alert_loud", True)
    kb.button(text=f"{_chk(loud)} Звук 24/7",
              callback_data=SetCB(sec="mon", act="toggle", key="app.monitoring.service_failure_alert_loud"))
    kb.adjust(2)
    kb.row(_back())
    return kb.as_markup()


# ── 💾 Бэкапы ────────────────────────────────────────────────────────────────

def _day_label(day: int) -> str:
    """«1-е», «2-е», «3-е», «7-е» — число месяца с окончанием."""
    d = int(day)
    return f"{d}-е"


def backup_when_label(day: int, hour: int) -> str:
    return f"✏️ {_day_label(day)}, {int(hour):02d}:00"


def settings_backup(encryption: bool = False) -> InlineKeyboardMarkup:
    """Автобэкапы тумблером и «🔐 Шифрование» — всегда (фраза нужна и для
    восстановления шифрованных копий); включены — канал циклом, день и час
    одной кнопкой, «сделать сейчас»."""
    s = settings
    kb = InlineKeyboardBuilder()
    on = s.get_bool("app.scheduler.backup_enabled", True)
    kb.button(text=f"{_chk(on)} Автобэкапы",
              callback_data=SetCB(sec="backup", act="toggle", key="app.scheduler.backup_enabled"))
    kb.button(text="🔐 Шифрование", callback_data=SetCB(sec="backup", act="do", key="enc"))
    rows = [2]
    if on:
        ch = str(s.get("app.scheduler.backup_channel", "telegram") or "telegram").lower()
        kb.add(_cycle("backup", "app.scheduler.backup_channel",
                      "📨 Куда: " + ("E-mail" if ch == "email" else "Telegram")))
        kb.button(text=backup_when_label(s.get_int("app.scheduler.backup_day", 1),
                                         s.get_int("app.scheduler.backup_hour", 12)),
                  callback_data=SetCB(sec="backup", act="edit", key="backup_when"))
        kb.button(text="💾 Сделать сейчас", callback_data=SetCB(sec="backup", act="do", key="now"))
        rows += [2, 1]
    kb.adjust(*rows)
    kb.row(_back())
    return kb.as_markup()


def backup_encryption_kb(has_secret: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Сменить фразу" if has_secret else "🔑 Задать фразу",
              callback_data=SetCB(sec="backup", act="do", key="enc_set"))
    kb.button(text="⬅️ Назад", callback_data=SetCB(sec="backup"))
    kb.adjust(1)
    return kb.as_markup()


def restore_confirm(gateway: bool = False) -> InlineKeyboardMarkup:
    """«Отмена» первой: восстановление необратимо, промах пальцем не должен
    возвращать всех на неделю назад."""
    if gateway:
        return confirm(GwCB(action="restore_drop"), "♻️ Восстановить", GwCB(action="restore!"))
    return confirm(SetCB(sec="backup", act="do", key="restore_drop"), "♻️ Восстановить",
                   SetCB(sec="backup", act="do", key="restore!"))


# ── 🔧 Сервис ────────────────────────────────────────────────────────────────

def settings_svc(migration: str = "", available: bool = False,
                 orphans: int = 0) -> InlineKeyboardMarkup:
    """Перезапуски парой; переезд — по состоянию: идёт — кто не переехал,
    завершить и отменить; нет — начать (если настроен) и переехавшие после
    отмены (если есть)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Перезапуск AWG", callback_data=SetCB(sec="svc", act="do", key="awg"))
    kb.button(text="🔁 Перезапуск бота", callback_data=SetCB(sec="svc", act="do", key="bot"))
    rows = [2]
    if available:
        if migration:
            kb.button(text="👥 Кто не переехал", callback_data=SetCB(sec="mig", act="do", key="pending"))
            kb.button(text="✅ Завершить", callback_data=SetCB(sec="mig", act="do", key="finish"))
            kb.button(text="↩️ Отменить", callback_data=SetCB(sec="mig", act="do", key="cancel"))
            rows += [1, 2]
        else:
            kb.button(text="🚚 Начать переезд", callback_data=SetCB(sec="mig", act="do", key="start"))
            rows.append(1)
            if orphans:
                kb.button(text=f"⚠️ Переехавшие после отмены: {orphans}",
                          callback_data=SetCB(sec="mig", act="do", key="orphans"))
                rows.append(1)
    kb.adjust(*rows)
    kb.row(_back())
    return kb.as_markup()


_MIG_CONFIRM_LABEL = {"start": "🚚 Начать", "finish": "✅ Завершить",
                      "cancel": "↩️ Отменить"}


def svc_confirm(key: str) -> InlineKeyboardMarkup:
    """Подтверждение перезапуска AWG / бота: «Отмена» первой."""
    return confirm(SetCB(sec="svc", act="open"), "🔁 Перезапустить", SetCB(sec="svc", act="do", key=f"{key}!"))


def migration_confirm(key: str) -> InlineKeyboardMarkup:
    """Подтверждение входа в переезд и обоих выходов: «Отмена» первой."""
    return confirm(SetCB(sec="svc", act="open"), _MIG_CONFIRM_LABEL[key], SetCB(sec="mig", act="do", key=f"{key}!"))


# ── ⬆️ Обновления ────────────────────────────────────────────────────────────

UPDATE_SCHEDULE_CYCLE = ("day", "week", "month")
UPDATE_SCHEDULE_LABELS = {"day": "день", "week": "неделя", "month": "месяц"}


def settings_updates(muted: bool, target_tag: str = "", blocked: str = "") -> InlineKeyboardMarkup:
    """target_tag — найденная цель обновления (кнопка «⬆️ Обновить до vX»,
    если не заблокирована); «Уведомлять» — мьют в БД; «Проверка» — цикл
    день → неделя → месяц."""
    s = settings
    kb = InlineKeyboardBuilder()
    rows = []
    if target_tag and not blocked:
        tag = target_tag if str(target_tag).startswith("v") else f"v{target_tag}"
        kb.button(text=f"⬆️ Обновить до {tag}", callback_data=UpdateCB(action="install"))
        rows.append(1)
    sched = str(s.get("updates.poll_schedule", "day")).lower()
    if sched not in UPDATE_SCHEDULE_LABELS:
        sched = "month"
    kb.button(text=f"{_chk(not muted)} Уведомлять", callback_data=SetCB(sec="upd", act="toggle", key="notify"))
    kb.add(_cycle("upd", "updates.poll_schedule", f"📅 Проверка: {UPDATE_SCHEDULE_LABELS[sched]}"))
    rows.append(2)
    kb.adjust(*rows)
    kb.row(_back())
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


def migration_needed() -> InlineKeyboardMarkup:
    """Инфобокс «нужен переезд»: сразу к подтверждению старта и «Скрыть».
    Скрыть — не «отложить навсегда»: сообщение приходит при каждом старте,
    пока переезд не начат."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🚚 Начать переезд", callback_data=SetCB(sec="mig", act="do", key="start"))
    kb.button(text="Скрыть", callback_data=HideCB())
    kb.adjust(1)
    return kb.as_markup()


def update_done_menu() -> InlineKeyboardMarkup:
    """«В меню» на итоговом сообщении self-update. Свой колбэк (upd:menu), а не
    Menu(main): стандартный обработчик РЕДАКТИРУЕТ сообщение в панель, а итог
    должен остаться в истории — кнопка лишь снимается, меню приходит новым
    сообщением."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В меню", callback_data=UpdateCB(action="menu"))
    return kb.as_markup()
