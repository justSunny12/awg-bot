"""Роль gateway (агент на шлюзе): панель, «🔀 VPN-транзит», настройки, бандл."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from awgbot.bot import ui
from awgbot.bot.callbacks import GwCB

from .common import entry_tag, confirm


def gateway_panel_kb(lan: bool = False, pending: int = 0, update_tag: str = "",
                     broken: bool = False) -> InlineKeyboardMarkup:
    """Панель шлюза: [⏳ Ответить (N)] (ждут вопросы с сервера AWG) / [🩺 Здоровье]
    [🚑 Восстановить] (только при проблемах — иначе кнопка дублирует ту, что
    есть в «🩺 Здоровье»; решение 16.10.2026) / [🔀 VPN-транзит] (когда включён) / [⬆️ vX] (доступно
    обновление — ведёт в «⬆️ Обновления») [⚙️ Настройки]. Перечитать панель
    живьём — ссылкой «обновлено … 🔄» в тексте, кнопки нет. Восстановление —
    через подтверждение: обрыв РФ у всех, пусть на секунды, не от промаха пальцем."""
    n = int(pending or 0)
    tag = (update_tag if str(update_tag).startswith("v") else f"v{update_tag}") if update_tag else ""
    return ui.rows(
        (f"⏳ Ответить ({n})", GwCB(action="cfg_ask")) if n else None,
        [("🩺 Здоровье", GwCB(action="health")),
         ("🚑 Восстановить", GwCB(action="reassert")) if broken else None],
        ("🔀 VPN-транзит", GwCB(action="lan")) if lan else None,
        [(f"⬆️ {tag}", GwCB(action="updates")) if tag else None, ("⚙️ Настройки", GwCB(action="settings"))])


def gateway_cfg_question_kb(section: str) -> InlineKeyboardMarkup:
    """Приглашение (ключи линка, аплинк) — «⬅️ Отмена» и «Применить» одним рядом.
    Вопрос о почте или фразе — одна пара на все вопросы «своё или с сервера»:
    «⬅️ Оставить своё» и «✅ Принять». «⬅️ В меню» последним рядом: не готов
    решать — вопрос остаётся в «⏳ Ждут решения»."""
    from awgbot.bot.texts.gateway import ask as _ask
    from awgbot.bot.texts.gateway.ask import KEEP_LABEL, ACCEPT_LABEL
    if section in ("keep", "keep_dev"):
        keep, accept = ((_ask.RESTORE_KEEP_LABEL, _ask.RESTORE_ACCEPT_LABEL) if section == "keep"
                        else (_ask.DEVICE_KEEP_LABEL, _ask.DEVICE_ACCEPT_LABEL))
        # подписи длинные — по одной в ряд, «оставить» первой
        return ui.rows((keep, GwCB(action="cfg_no", val=section)), (accept, GwCB(action="cfg_ok!", val=section)),
                       ui.to_menu(GwCB(action="panel")))
    if section in ("link", "uplink"):
        icon = "🔑" if section == "link" else "🔐"
        pair = [("⬅️ Отмена", GwCB(action="cfg_no", val=section)),
                (f"{icon} Применить", GwCB(action="cfg_ok!", val=section))]
    else:
        pair = [(KEEP_LABEL, GwCB(action="cfg_no", val=section)),
                (ACCEPT_LABEL, GwCB(action="cfg_ok!", val=section))]
    return ui.rows(pair, ui.to_menu(GwCB(action="panel")))


def gateway_health_kb() -> InlineKeyboardMarkup:
    """Экран здоровья: проверить заново (живьём), восстановить; в меню."""
    return ui.rows([("🔄 Проверить", GwCB(action="health")),
                    ("🚑 Восстановить", GwCB(action="reassert", val="health"))],
                   ui.to_menu(GwCB(action="panel")))


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
    """Экран «🔀 VPN-транзит»: добавить в туннель (🌍) / напрямую (🇷🇺) — значки
    те же, что у кнопок «➖ 🇷🇺|🌍 домен» (листание, не больше десяти рядов на
    экране), настройка роутера и в меню. val — номер в отсортированном списке,
    не домен (64 байта)."""
    return ui.rows(
        [("➕ 🌍 В туннель", GwCB(action="lan_add")), ("➕ 🇷🇺 Напрямую", GwCB(action="lan_ru"))],
        *ui.paged(lan_own_sorted(list(items)), page, static=2, screen="lanlist", ref=0,
                  back=GwCB(action="lan").pack(),
                  button=lambda i, kd: (f"➖ {'🇷🇺' if kd[0] == 'ru' else '🌍'} {kd[1]}",
                                        GwCB(action="lan_rm", val=f"{i}.{lan_own_tag(kd[0], kd[1])}"))),
        [("🛜 Роутер", GwCB(action="lan_router")), ui.to_menu(GwCB(action="panel"))])


def _gw_back(sec: str = "settings") -> tuple:
    return ui.back(GwCB(action=sec))


def gateway_ssh_kb(st: dict, page: int = 0) -> InlineKeyboardMarkup:
    """«🛡 SSH-доступ» агента — зеркало раздела основного бота: порт, адреса
    (val — номер записи по полному списку), тумблер фильтра снаружи. На
    обвязке старого образца тумблера нет — включать нечего."""
    toggle = bool(st.get("new_plumbing"))
    return ui.rows(
        [("🅿️ Порт", GwCB(action="ssh_port")), ("➕ Адрес", GwCB(action="ssh_add"))],
        *ui.paged(list(st.get("allow") or []), page, static=3 if toggle else 2, screen="gwssh", ref=0,
                  back=GwCB(action="ssh").pack(),
                  button=lambda i, entry: (f"➖ {entry}", GwCB(action="ssh_del!", val=f"{i}.{entry_tag(entry)}"))),
        (("✅ Фильтр снаружи", GwCB(action="ssh_off!")) if st.get("filter")
         else ("☑️ Фильтр снаружи", GwCB(action="ssh_on!"))) if toggle else None,
        _gw_back("settings"))


def gateway_ssh_port_finisher_kb() -> InlineKeyboardMarkup:
    """Финишер порта SSH агента — как у основного: другой порт или назад."""
    return ui.rows([("✏️ Другой порт", GwCB(action="ssh_port_retry")),
                    ui.back(GwCB(action="ssh_port_back"))])


def gateway_confirm_kb(action: str, back: str = "panel") -> InlineKeyboardMarkup:
    """«Отмена» первой — необратимое не там, куда палец идёт по инерции.
    back — куда возвращает отказ (восстановление живёт на панели и экране
    здоровья); перезапуски — в «🔧 Сервисе» общих разделов."""
    label = {"reassert": "🚑 Восстановить"}.get(action, "🔁 Перезапустить")
    return confirm(GwCB(action=back), label, GwCB(action=f"{action}!"))


def gateway_back_kb(sec: str = "") -> InlineKeyboardMarkup:
    """Одна кнопка назад: без sec — «В меню» (панель), с sec — «Назад» в раздел
    (отказ смены порта возвращает в «SSH-доступ»)."""
    return ui.rows(_gw_back(sec) if sec else ui.to_menu(GwCB(action="panel")))


def gateway_transit_router_kb(tab: str = "mt") -> InlineKeyboardMarkup:
    """Настройка роутера вкладками; назад — на экран «🔀 VPN-транзит»."""
    from awgbot.bot.texts.routing import ROUTER_TABS
    return ui.rows(
        [(f"✅ {label}" if key == tab else label, GwCB(action="lan_router", val=key)) for key, label in ROUTER_TABS],
        ui.back(GwCB(action="lan")))
