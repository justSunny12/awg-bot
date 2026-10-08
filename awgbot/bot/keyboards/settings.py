"""Экран «⚙️ Настройки» (админ): ролевые разделы — сервер, SSH-доступ, подписки, переезд, свой резолвер;
общие разделы обеих ролей — в bot/sections/."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from awgbot.core import settings
from awgbot.bot.callbacks import Menu, UpdateCB, SetCB, HideCB

from .common import paged_rows, entry_tag, confirm


# ─────────────────────────────────────────────────────────────────────────────
# Экран «⚙️ Настройки» (админ). Значения читаются из settings в момент рендера —
# после правки экран перерисовывается и показывает актуальное.
# ─────────────────────────────────────────────────────────────────────────────


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
    """Кнопка-цикл ролевого раздела: нажатие переставляет значение на следующее
    из ряда (обработчик act="cycle"), подпись — текущее значение."""
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
