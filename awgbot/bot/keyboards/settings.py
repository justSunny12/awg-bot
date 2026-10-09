"""Экран «⚙️ Настройки» (админ): ролевые разделы — сервер, SSH-доступ, подписки, переезд, свой резолвер;
общие разделы обеих ролей — в bot/sections/."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from awgbot.core import settings
from awgbot.bot import ui
from awgbot.bot.callbacks import Menu, UpdateCB, SetCB, HideCB

from .common import entry_tag, confirm


# ─────────────────────────────────────────────────────────────────────────────
# Экран «⚙️ Настройки» (админ). Значения читаются из settings в момент рендера —
# после правки экран перерисовывается и показывает актуальное.
# ─────────────────────────────────────────────────────────────────────────────


def restart_now_or_later() -> InlineKeyboardMarkup:
    """Под итогом развёртывания обвязки и подъёма интерфейса переезда: бот
    читает их при старте — перезапуск сейчас или позже (⚙️ → 🔧 Сервис)."""
    return ui.rows(("🔁 Перезапустить сейчас", SetCB(sec="svc", act="do", key="bot!")),
                   ("⬅️ Позже", Menu(action="main")))


def settings_back(sec_to: str = "root") -> InlineKeyboardMarkup:
    """Одна кнопка «Назад» — для экранов-отбивок внутри настроек."""
    return ui.rows(_back(sec_to))


def _back(sec_to: str = "root") -> tuple:
    return ui.back(SetCB(sec=sec_to))


def _cycle(sec: str, key: str, label: str) -> InlineKeyboardButton:
    """Кнопка-цикл ролевого раздела: нажатие переставляет значение на следующее
    из ряда (обработчик act="cycle"), подпись — текущее значение."""
    return ui.btn(label, SetCB(sec=sec, act="cycle", key=key))


# ── 🖥 Сервер AWG ────────────────────────────────────────────────────────────

def settings_server(blocked: str = "", private_dns_offer: bool = False) -> InlineKeyboardMarkup:
    """Раздел «Сервер»: то, что уезжает в НОВЫЕ ссылки. Порт и подсеть
    меняются переездом — кнопка ведёт на экран, который называет цену."""
    return ui.rows(
        [("✏️ Домен", SetCB(sec="srv", act="edit", key="app.network.server_host")),
         ("✏️ Имя", SetCB(sec="srv", act="edit", key="app.client_config.server_name"))],
        [("✏️ DNS", SetCB(sec="srv", act="edit", key="app.client_config.dns1")),
         ("✏️ MTU", SetCB(sec="srv", act="edit", key="app.client_config.mtu"))],
        [("🔒 Свой резолвер", SetCB(sec="dns", act="open")) if private_dns_offer else None,
         ("🚚 Порт, подсеть", SetCB(sec="mig_prep", act="open")) if not blocked else None],
        _back())


def private_dns_choices(migration_blocked: bool = False) -> InlineKeyboardMarkup:
    """Три решения; «сейчас» ведёт в подготовку переезда — пока переезд
    возможен."""
    return ui.rows(
        ("🚚 Переехать сейчас", SetCB(sec="dns", act="do", key="now")) if not migration_blocked else None,
        [("⏳ При переезде", SetCB(sec="dns", act="do", key="later")),
         ("Не нужно", SetCB(sec="dns", act="do", key="never"))],
        ui.back(SetCB(sec="srv", act="open")))


def private_dns_offer_kb() -> InlineKeyboardMarkup:
    """Инфобокс при старте: те же три решения, «Назад» не нужен."""
    return ui.rows(
        ("🚚 Переехать сейчас", SetCB(sec="dns", act="do", key="now")),
        [("⏳ При переезде", SetCB(sec="dns", act="do", key="later")),
         ("Не нужно", SetCB(sec="dns", act="do", key="never"))])


def migration_prepare_confirm(want_port: int = 0) -> InlineKeyboardMarkup:
    return ui.rows(
        ("🚚 Поднять интерфейс", SetCB(sec="mig_prep", act="do", key="go", val=str(want_port or ""))),
        [("✏️ Свой порт", SetCB(sec="mig_prep", act="edit", key="port")),
         ("✖️ Отмена", SetCB(sec="srv", act="open"))])


def migration_generation_pending() -> InlineKeyboardMarkup:
    """Кнопка на сообщении «ядро нового поколения ждёт переезда»."""
    return ui.rows(("🚚 Подготовить переезд", SetCB(sec="mig_prep", act="do", key="go")),
                   ("Скрыть", HideCB()))


# ── 🛡 SSH-доступ ────────────────────────────────────────────────────────────

def settings_firewall(st: dict, page: int = 0) -> InlineKeyboardMarkup:
    """Раздел «SSH-доступ». «Фильтр снаружи» — тумблер: включить можно только
    при адресах в списке (фильтр без адресов открывает SSH всем)."""
    # В callback_data уезжает НОМЕР записи (по полному списку), а не сам адрес:
    # разделитель полей — двоеточие, и любой IPv6 ломал бы упаковку.
    allow = list(st.get("raw_allow", []) or [])
    toggle = bool(st.get("enabled")) or bool(allow)
    return ui.rows(
        [("🅿️ Порт", SetCB(sec="fw", act="edit", key="port")),
         ("➕ Адрес", SetCB(sec="fw", act="edit", key="app.firewall.ssh_allow"))],
        *ui.paged(allow, page, static=3 if toggle else 2, screen="fw", ref=0, back=SetCB(sec="fw").pack(),
                  button=lambda i, entry: (f"➖ {entry}", SetCB(sec="fw", act="do", key="del", val=f"{i}.{entry_tag(entry)}"))),
        ("✅ Фильтр снаружи", SetCB(sec="fw", act="do", key="off")) if st.get("enabled") else
        ("☑️ Фильтр снаружи", SetCB(sec="fw", act="do", key="on")) if allow else None,
        _back())


def ssh_port_finisher() -> InlineKeyboardMarkup:
    """Финишер «порт не изменился / не выполнена»: другой порт или назад в
    раздел; нажатие оставляет финишер с одной «Скрыть»."""
    return ui.rows([("✏️ Другой порт", SetCB(sec="fw", act="do", key="port_retry")),
                    ui.back(SetCB(sec="fw", act="do", key="port_back"))])


# ── 💳 Подписки ──────────────────────────────────────────────────────────────

def settings_subs() -> InlineKeyboardMarkup:
    s = settings
    return ui.rows(
        [(f"📈 Бонус: {s.get_int('limits.traffic_bonus_gb', 100)} ГБ",
          SetCB(sec="subs", act="edit", key="limits.traffic_bonus_gb")),
         (f"🙏 Отсрочка: {s.get_int('grace.grace_days', 14)} дн.",
          SetCB(sec="subs", act="edit", key="grace.grace_days"))],
        [(f"⏸️ Год: {s.get_int('pause.pause_max_total_days', 28)} дн.",
          SetCB(sec="subs", act="edit", key="pause.pause_max_total_days")),
         (f"⏸️ Месяц: {s.get_int('pause.monthly_pause_days', 2)} дн.",
          SetCB(sec="subs", act="edit", key="pause.monthly_pause_days"))],
        _back())


# ── 🔧 Сервис ────────────────────────────────────────────────────────────────


_MIG_CONFIRM_LABEL = {"start": "🚚 Начать", "finish": "✅ Завершить",
                      "cancel": "↩️ Отменить"}


def migration_confirm(key: str) -> InlineKeyboardMarkup:
    """Подтверждение входа в переезд и обоих выходов: «Отмена» первой."""
    return confirm(SetCB(sec="svc", act="open"), _MIG_CONFIRM_LABEL[key], SetCB(sec="mig", act="do", key=f"{key}!"))


def update_notify() -> InlineKeyboardMarkup:
    """Кнопки уведомления о новой версии: Обновить / Скрыть / Не уведомлять.
    «Скрыть» — универсальная HideCB (удаляет сообщение); «один раз на версию»
    держит notified_tag в БД, не кнопка."""
    return ui.rows([("⬆️ Обновить", UpdateCB(action="install")), ("Скрыть", HideCB())],
                   ("🔕 Не уведомлять об обновлениях", UpdateCB(action="mute")))


def migration_needed() -> InlineKeyboardMarkup:
    """Инфобокс «нужен переезд»: сразу к подтверждению старта и «Скрыть».
    Скрыть — не «отложить навсегда»: сообщение приходит при каждом старте,
    пока переезд не начат."""
    return ui.rows(("🚚 Начать переезд", SetCB(sec="mig", act="do", key="start")),
                   ("Скрыть", HideCB()))


def update_done_menu() -> InlineKeyboardMarkup:
    """«В меню» на итоговом сообщении self-update. Свой колбэк (upd:menu), а не
    Menu(main): стандартный обработчик РЕДАКТИРУЕТ сообщение в панель, а итог
    должен остаться в истории — кнопка лишь снимается, меню приходит новым
    сообщением."""
    return ui.rows(ui.to_menu(UpdateCB(action="menu")))
