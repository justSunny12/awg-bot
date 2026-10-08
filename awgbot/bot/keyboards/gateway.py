"""Роль gateway (агент на шлюзе): панель, «🔀 VPN-транзит», настройки, бандл."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from awgbot.bot.callbacks import GwCB

from .common import paged_rows, entry_tag, confirm


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


def _gw_back(sec: str = "settings") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="⬅️ Назад", callback_data=GwCB(action=sec).pack())


def link_minutes(seconds: int) -> int:
    """Порог молчания линка в минутах — вверх: 90 с показываем как 2 мин, а
    не как 1, иначе алерт кажется более ранним, чем есть."""
    return max(1, -(-int(seconds or 0) // 60))


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
    """Финишер порта SSH агента — как у основного: другой порт или назад."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Другой порт", callback_data=GwCB(action="ssh_port_retry"))
    kb.button(text="⬅️ Назад", callback_data=GwCB(action="ssh_port_back"))
    kb.adjust(2)
    return kb.as_markup()


def gateway_confirm_kb(action: str, back: str = "panel") -> InlineKeyboardMarkup:
    """«Отмена» первой — необратимое не там, куда палец идёт по инерции.
    back — куда возвращает отказ (восстановление живёт на панели и экране
    здоровья); перезапуски — в «🔧 Сервисе» общих разделов."""
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

