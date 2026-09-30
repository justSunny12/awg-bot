"""Роль gateway (агент на шлюзе): панель, «🔀 VPN-транзит», настройки, бандл."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from awgbot.core import settings
from awgbot.bot.callbacks import UpdateCB, GwCB

from .common import paged_rows, _chk, entry_tag, confirm
from .settings import backup_when_label, UPDATE_SCHEDULE_LABELS


def gateway_panel_kb(lan: bool = False) -> InlineKeyboardMarkup:
    """Панель шлюза: [🩺 Здоровье] [🔧 Восстановить] / [🔀 VPN-транзит] (когда
    включён) / [🔄 Обновить] [⚙️ Настройки]. Восстановление — через
    подтверждение: обрыв РФ у всех, пусть на секунды, не от промаха пальцем."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🩺 Здоровье", callback_data=GwCB(action="health"))
    kb.button(text="🔧 Восстановить", callback_data=GwCB(action="reassert"))
    rows = [2]
    if lan:
        kb.button(text="🔀 VPN-транзит", callback_data=GwCB(action="lan"))
        rows.append(1)
    kb.button(text="🔄 Обновить", callback_data=GwCB(action="refresh"))
    kb.button(text="⚙️ Настройки", callback_data=GwCB(action="settings"))
    rows.append(2)
    kb.adjust(*rows)
    return kb.as_markup()


def gateway_health_kb() -> InlineKeyboardMarkup:
    """Экран здоровья: восстановить и в меню."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔧 Восстановить", callback_data=GwCB(action="reassert", val="health"))
    kb.button(text="⬅️ В меню", callback_data=GwCB(action="panel"))
    kb.adjust(2)
    return kb.as_markup()


def lan_own_sorted(items) -> list[tuple[str, str]]:
    """Свои списки одним порядком для экрана и колбэков: сначала «напрямую»
    (🇷🇺), затем «в туннель» (🌍), внутри — по алфавиту."""
    return sorted(items, key=lambda kd: (0 if kd[0] == "ru" else 1, kd[1]))


def lan_own_tag(kind: str, dom: str) -> str:
    """Короткая метка записи для колбэка: домен целиком в 64 байта не влезает,
    а номер один сменит хозяина, стоит списку измениться (синхронизация с
    других шлюзов, консоль awg-bot lan)."""
    import hashlib
    return hashlib.sha1(f"{kind} {dom}".encode()).hexdigest()[:8]


def gateway_transit_kb(items=(), page: int = 0) -> InlineKeyboardMarkup:
    """Экран «🔀 VPN-транзит»: добавить в туннель / напрямую, свои домены
    кнопками «➖ 🇷🇺|🌍 домен» (листание, не больше десяти рядов на экране), рецепт
    роутера и в меню. val — номер в отсортированном списке, не домен (64 байта)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ В туннель", callback_data=GwCB(action="lan_add"))
    kb.button(text="➕ Напрямую", callback_data=GwCB(action="lan_ru"))
    rows = paged_rows(kb, lan_own_sorted(list(items)), page, static=2, screen="lanlist", ref=0,
                      back=GwCB(action="lan").pack(),
                      button=lambda i, kd: kb.button(text=f"➖ {'🇷🇺' if kd[0] == 'ru' else '🌍'} {kd[1]}",
                                                     callback_data=GwCB(action="lan_rm", val=f"{i}.{lan_own_tag(kd[0], kd[1])}")))
    kb.button(text="❓ Роутер", callback_data=GwCB(action="lan_router"))
    kb.button(text="⬅️ В меню", callback_data=GwCB(action="panel"))
    kb.adjust(2, *rows, 2)
    return kb.as_markup()


def gateway_settings_kb() -> InlineKeyboardMarkup:
    """Корень настроек агента в два столбца; перезапуски — здесь же,
    «Обслуживания» больше нет."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔔 Уведомления", callback_data=GwCB(action="notify"))
    kb.button(text="✉️ E-mail", callback_data=GwCB(action="email"))
    kb.button(text="🛡 SSH-доступ", callback_data=GwCB(action="ssh"))
    kb.button(text="🩺 Мониторинг", callback_data=GwCB(action="mon"))
    kb.button(text="💾 Бэкапы", callback_data=GwCB(action="backup"))
    kb.button(text="⬆️ Обновления", callback_data=GwCB(action="updates"))
    kb.button(text="🔁 Перезапуск AWG", callback_data=GwCB(action="restart"))
    kb.button(text="🔁 Перезапуск бота", callback_data=GwCB(action="botrestart"))
    kb.button(text="⬅️ В меню", callback_data=GwCB(action="panel"))
    kb.adjust(2, 2, 2, 2, 1)
    return kb.as_markup()


def _gw_back(sec: str = "settings") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="⬅️ Назад", callback_data=GwCB(action=sec).pack())


def link_minutes(seconds: int) -> int:
    """Порог молчания линка в минутах — вверх: 90 с показываем как 2 мин, а
    не как 1, иначе алерт кажется более ранним, чем есть."""
    return max(1, -(-int(seconds or 0) // 60))


def _cyc(key: str, label: str) -> InlineKeyboardButton:
    """Кнопка-цикл агента: следующее значение ряда (обработчик «cyc»)."""
    return InlineKeyboardButton(text=label, callback_data=GwCB(action="cyc", val=key).pack())


def gateway_notify_kb() -> InlineKeyboardMarkup:
    """Тихие часы и границы, алерты и четыре порога, аварии на e-mail — в
    один ряд с «Назад»."""
    s = settings
    kb = InlineKeyboardBuilder()
    rows = []
    qh = s.get_bool("quiet_hours.quiet_hours_enabled", True)
    kb.button(text=f"{_chk(qh)} Тихие часы", callback_data=GwCB(action="tgl", val="quiet_hours.quiet_hours_enabled"))
    rows.append(1)
    if qh:
        kb.button(text=f"С {s.get_int('quiet_hours.quiet_hours_start', 20):02d}:00",
                  callback_data=GwCB(action="edit", val="quiet_hours.quiet_hours_start"))
        kb.button(text=f"До {s.get_int('quiet_hours.quiet_hours_end', 7):02d}:00",
                  callback_data=GwCB(action="edit", val="quiet_hours.quiet_hours_end"))
        rows.append(2)
    ra = s.get_bool("resource_alerts.enabled", True)
    kb.button(text=f"{_chk(ra)} Алерты хоста", callback_data=GwCB(action="tgl", val="resource_alerts.enabled"))
    rows.append(1)
    if ra:
        kb.button(text=f"CPU {s.get_int('resource_alerts.thresholds_percent.cpu', 80)}%",
                  callback_data=GwCB(action="edit", val="resource_alerts.thresholds_percent.cpu"))
        kb.button(text=f"RAM {s.get_int('resource_alerts.thresholds_percent.ram', 80)}%",
                  callback_data=GwCB(action="edit", val="resource_alerts.thresholds_percent.ram"))
        kb.button(text=f"Диск {s.get_int('resource_alerts.thresholds_percent.disk', 80)}%",
                  callback_data=GwCB(action="edit", val="resource_alerts.thresholds_percent.disk"))
        kb.button(text=f"{s.get_int('app.gateway.temp_alert_c', 75)} °C",
                  callback_data=GwCB(action="edit", val="app.gateway.temp_alert_c"))
        rows += [2, 2]
    ef = s.get_bool("notifications.email_fallback", False)
    kb.button(text=f"{_chk(ef)} Аварии на e-mail", callback_data=GwCB(action="tgl", val="notifications.email_fallback"))
    kb.add(_gw_back())
    rows.append(2)
    kb.adjust(*rows)
    return kb.as_markup()


def gateway_mon_kb() -> InlineKeyboardMarkup:
    """«Мониторинг» агента: опрос, замеров до алерта, порог молчания линка в
    минутах (хранится в секундах), звук 24/7."""
    s = settings
    kb = InlineKeyboardBuilder()
    kb.button(text=f"⏱ Опрос: {s.get_int('app.gateway.monitor_minutes', 3)} мин",
              callback_data=GwCB(action="edit", val="app.gateway.monitor_minutes"))
    kb.button(text=f"🔢 Замеров: {s.get_int('app.monitoring.alert_streak', 5)}",
              callback_data=GwCB(action="edit", val="app.monitoring.alert_streak"))
    kb.button(text=f"⏳ Линк: {link_minutes(s.get_int('app.gateway.handshake_max_age', 300))} мин",
              callback_data=GwCB(action="edit", val="app.gateway.handshake_max_age"))
    loud = s.get_bool("app.gateway.link_alert_loud", True)
    kb.button(text=f"{_chk(loud)} Звук 24/7", callback_data=GwCB(action="tgl", val="app.gateway.link_alert_loud"))
    kb.adjust(2, 2)
    kb.row(_gw_back())
    return kb.as_markup()


def gateway_backup_kb(encryption: bool = False) -> InlineKeyboardMarkup:
    """Как у основного бота: автобэкапы и шифрование всегда, канал циклом,
    день и час одной кнопкой, «сделать сейчас» — при включённых."""
    s = settings
    kb = InlineKeyboardBuilder()
    on = s.get_bool("app.scheduler.backup_enabled", True)
    kb.button(text=f"{_chk(on)} Автобэкапы", callback_data=GwCB(action="tgl", val="app.scheduler.backup_enabled"))
    kb.button(text="🔐 Шифрование", callback_data=GwCB(action="enc"))
    rows = [2]
    if on:
        ch = str(s.get("app.scheduler.backup_channel", "telegram") or "telegram").lower()
        kb.add(_cyc("app.scheduler.backup_channel", "📨 Куда: " + ("E-mail" if ch == "email" else "Telegram")))
        kb.button(text=backup_when_label(s.get_int("app.scheduler.backup_day", 1),
                                         s.get_int("app.scheduler.backup_hour", 12)),
                  callback_data=GwCB(action="edit", val="backup_when"))
        kb.button(text="💾 Сделать сейчас", callback_data=GwCB(action="backup!"))
        rows += [2, 1]
    kb.adjust(*rows)
    kb.row(_gw_back())
    return kb.as_markup()


def gateway_email_kb(configured: bool) -> InlineKeyboardMarkup:
    """Почта у агента: те же кнопки, что у основного, без аварийного выхода."""
    kb = InlineKeyboardBuilder()
    if not configured:
        kb.button(text="✉️ Подключить ящик", callback_data=GwCB(action="em_setup"))
        kb.adjust(1)
    else:
        kb.button(text="🔍 Проверить", callback_data=GwCB(action="em_check"))
        kb.button(text="📨 Тест-письмо", callback_data=GwCB(action="em_test"))
        kb.button(text="✏️ Сменить ящик", callback_data=GwCB(action="em_setup"))
        kb.button(text="🗑 Отключить", callback_data=GwCB(action="em_forget"))
        kb.adjust(2, 2)
    kb.row(_gw_back())
    return kb.as_markup()


def gateway_email_forget_confirm() -> InlineKeyboardMarkup:
    return confirm(GwCB(action="email"), "🗑 Отключить", GwCB(action="em_forget!"))


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
    kb.button(text="⬅️ Назад", callback_data=GwCB(action="backup"))
    kb.adjust(1)
    return kb.as_markup()


def gateway_ssh_kb(st: dict, page: int = 0) -> InlineKeyboardMarkup:
    """«🛡 SSH-доступ» агента — зеркало раздела основного бота: порт, адреса
    (val — номер записи по полному списку), тумблер фильтра снаружи. На
    обвязке старого образца тумблера нет — включать нечего."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🅿️ Порт", callback_data=GwCB(action="ssh_port"))
    kb.button(text="➕ Адрес", callback_data=GwCB(action="ssh_add"))
    toggle = bool(st.get("new_plumbing"))
    rows = [2, *paged_rows(kb, list(st.get("allow") or []), page, static=3 if toggle else 2, screen="gwssh", ref=0,
                           back=GwCB(action="ssh").pack(),
                           button=lambda i, entry: kb.button(text=f"➖ {entry}", callback_data=GwCB(action="ssh_del!", val=f"{i}.{entry_tag(entry)}")))]
    if toggle:
        if st.get("filter"):
            kb.button(text="✅ Фильтр снаружи", callback_data=GwCB(action="ssh_off!"))
        else:
            kb.button(text="☑️ Фильтр снаружи", callback_data=GwCB(action="ssh_on!"))
        rows.append(1)
    kb.adjust(*rows)
    kb.row(_gw_back("settings"))
    return kb.as_markup()


def gateway_ssh_port_finisher_kb() -> InlineKeyboardMarkup:
    """Финишер «порт не изменился / не выполнена»: другой порт или раздел;
    нажатие оставляет финишер с одной «Скрыть»."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Другой порт", callback_data=GwCB(action="ssh_port_retry"))
    kb.button(text="⬅️ Назад", callback_data=GwCB(action="ssh_port_back"))
    kb.adjust(2)
    return kb.as_markup()



def gateway_updates_kb(muted: bool, target_tag: str = "") -> InlineKeyboardMarkup:
    """Раздел обновлений агента — как у основного: «⬆️ Обновить до vX» при
    найденной цели, тумблер уведомлений, цикл расписания без «никогда»."""
    s = settings
    kb = InlineKeyboardBuilder()
    rows = []
    if target_tag:
        tag = target_tag if str(target_tag).startswith("v") else f"v{target_tag}"
        kb.button(text=f"⬆️ Обновить до {tag}", callback_data=UpdateCB(action="install"))
        rows.append(1)
    sched = str(s.get("updates.poll_schedule", "day")).lower()
    if sched not in UPDATE_SCHEDULE_LABELS:
        sched = "month"
    kb.button(text=f"{_chk(not muted)} Уведомлять", callback_data=GwCB(action="upd_toggle"))
    kb.add(_cyc("updates.poll_schedule", f"📅 Проверка: {UPDATE_SCHEDULE_LABELS[sched]}"))
    rows.append(2)
    kb.adjust(*rows)
    kb.row(_gw_back("settings"))
    return kb.as_markup()


def gateway_confirm_kb(action: str, back: str = "panel") -> InlineKeyboardMarkup:
    """«Отмена» первой — необратимое не там, куда палец идёт по инерции.
    back — куда возвращает отказ: восстановление живёт на панели,
    перезапуски — в настройках."""
    label = {"reassert": "🔧 Восстановить"}.get(action, "🔁 Перезапустить")
    return confirm(GwCB(action=back), label, GwCB(action=f"{action}!"))


def gateway_bundle_kb() -> InlineKeyboardMarkup:
    return confirm(GwCB(action="drop"), "📦 Применить", GwCB(action="apply!"), danger=False)


def gateway_bundle_passphrase_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Оставить свою", callback_data=GwCB(action="apply_keep!"))
    kb.button(text="🔐 Перезаписать", callback_data=GwCB(action="apply_ow!"))
    kb.adjust(2)
    return kb.as_markup()


def gateway_back_kb(sec: str = "") -> InlineKeyboardMarkup:
    """Одна кнопка назад: без sec — «В меню» (панель), с sec — «Назад» в раздел
    (отказ смены порта возвращает в «SSH-доступ»)."""
    kb = InlineKeyboardBuilder()
    if sec:
        kb.row(_gw_back(sec))
    else:
        kb.button(text="⬅️ В меню", callback_data=GwCB(action="panel"))
    return kb.as_markup()


def gateway_transit_router_kb(tab: str = "mt") -> InlineKeyboardMarkup:
    """Рецепт роутера вкладками; назад — на экран «🔀 VPN-транзит»."""
    from awgbot.bot.texts.routing import ROUTER_TABS
    kb = InlineKeyboardBuilder()
    for key, label in ROUTER_TABS:
        kb.button(text=(f"✅ {label}" if key == tab else label), callback_data=GwCB(action="lan_router", val=key))
    kb.button(text="⬅️ Назад", callback_data=GwCB(action="lan"))
    kb.adjust(len(ROUTER_TABS), 1)
    return kb.as_markup()

