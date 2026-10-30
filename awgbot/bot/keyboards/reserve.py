"""reserve.py — клавиатуры раздела «🛟 Резерв»: раздел без пары, приглашение
(живое сообщение), ввод строки и кода, диалог принятия, карточка резерва,
подтверждения."""
from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup

from awgbot.bot import ui
from awgbot.bot.callbacks import PeerCB, RsvCB, SetCB
from awgbot.bot.keyboards import common as kc


def _chk(on: bool) -> str:
    return "✅" if on else "☑️"


def _back(sec_to: str = "root"):
    return ui.back(SetCB(sec=sec_to))


def section(ready: bool, *, invite_live: bool, accepting: bool) -> InlineKeyboardMarkup:
    """Раздел без пары: готовность, пригласить / принять, запись DNS, назад."""
    busy = invite_live or accepting
    return ui.rows(
        (f"{_chk(ready)} Готов быть резервом: {'вкл' if ready else 'выкл'}", RsvCB(action="ready")),
        ("📤 Пригласить", RsvCB(action="invite")) if not busy else None,
        ("📥 Принять приглашение", RsvCB(action="accept")) if not busy else None,
        ("⬅️ Отменить приглашение", RsvCB(action="cancel")) if busy else None,
        ("🌐 DNS-запись", RsvCB(action="dns")),
        _back())


def invite_message(pid: str) -> InlineKeyboardMarkup:
    return ui.rows([("✏️ Ввести код", RsvCB(action="code", pid=pid)),
                    ("⬅️ Отменить", RsvCB(action="cancel", pid=pid))])


def code_prompt() -> InlineKeyboardMarkup:
    """«✖️ Отмена» под приглашением кода — ввод сброшен, на месте — раздел."""
    return kc.cancel_input("set_rsv", 0)


def accept_dialog(rf_on: bool, *, has_gateway: bool) -> InlineKeyboardMarkup:
    """«Отмена» первой: принять чужих пользователей — действие с последствиями."""
    return ui.rows(
        ("⬅️ Отмена", SetCB(sec="rsv")),
        (f"{_chk(rf_on)} Мои шлюзы для резерва", RsvCB(action="rf")) if has_gateway else None,
        ("🛟 Подключиться", RsvCB(action="connect")))


def accept_message() -> InlineKeyboardMarkup:
    return ui.rows(("⬅️ Отменить", RsvCB(action="cancel")))


def after_move() -> InlineKeyboardMarkup:
    """Итог перевода в живом сообщении: назад в раздел."""
    return ui.rows(_back("rsv"))


def final() -> InlineKeyboardMarkup:
    """Итог в живом сообщении: кнопок нет, остаётся след в чате."""
    return ui.rows()


def card(*, ready: bool, rf_on: bool, has_gateway: bool, ret_immediate: bool, name: str = "",
         can_move: bool = False, can_evict: bool = False, can_return: bool = False,
         self_admin: bool = False, can_force: bool = False) -> InlineKeyboardMarkup:
    """Кнопки переезда видны, только когда действие возможно; невозможное —
    строкой причины в карточке."""
    return ui.rows(
        (f"🔀 Переезд на «{name}»", RsvCB(action="move")) if can_move else None,
        ("↩️ Вернуть пользователей", RsvCB(action="back")) if can_return else None,
        (f"⚠️ Вернуть без «{name}»", RsvCB(action="force")) if can_force else None,
        (f"↩️ Вернуть всё на «{name}»", RsvCB(action="evict")) if can_evict else None,
        (f"{_chk(ready)} Готов быть резервом", RsvCB(action="ready")),
        (f"{_chk(rf_on)} Мои шлюзы для резерва", RsvCB(action="rfmine")) if has_gateway else None,
        ("⏱ Возврат: сразу" if ret_immediate else "⏱ Возврат: по кнопке", RsvCB(action="ret")),
        ("🌐 DNS-запись", RsvCB(action="dns")),
        (f"🛰 Для «{name}»", RsvCB(action="role")) if self_admin else None,
        ("🆘 Аварийный код", RsvCB(action="emerg")),
        ("🗑 Удалить резерв", RsvCB(action="unpair")),
        _back())


def confirm_rfmine_off() -> InlineKeyboardMarkup:
    """Т-РТ4: «⬅️ Отмена» первой, «Выключить» второй."""
    return kc.confirm(SetCB(sec="rsv"), "Выключить", RsvCB(action="rfmine_off"))


def confirm_move() -> InlineKeyboardMarkup:
    return kc.confirm(SetCB(sec="rsv"), "🔀 Перевести", RsvCB(action="move_yes"), danger=False)


def confirm_evict() -> InlineKeyboardMarkup:
    return kc.confirm(SetCB(sec="rsv"), "↩️ Вернуть", RsvCB(action="evict_yes"), danger=False)


def confirm_force() -> InlineKeyboardMarkup:
    return kc.confirm(SetCB(sec="rsv"), "⚠️ Да, вернуть", RsvCB(action="force_yes"), danger=True)


def confirm_return() -> InlineKeyboardMarkup:
    return kc.confirm(SetCB(sec="rsv"), "↩️ Вернуть", RsvCB(action="back_yes"), danger=False)


def dns(*, provider_label: str, has_name: bool, manual: bool) -> InlineKeyboardMarkup:
    """Т-Р16: провайдер циклом, токен, проверка, назад."""
    return ui.rows(
        (f"🏷 Провайдер: {provider_label}", RsvCB(action="dns_prov")) if has_name else None,
        [("✏️ Токен", RsvCB(action="dns_token")) if has_name and not manual else None,
         ("🧪 Проверить", RsvCB(action="dns_check")) if has_name else None],
        _back("rsv"))


def dns_token_prompt() -> InlineKeyboardMarkup:
    return ui.rows(("⬅️ Отмена", RsvCB(action="dns")))


def guest_card(*, can_return: bool) -> InlineKeyboardMarkup:
    """Карточка резерва в боте основы на резерве: вернуть пользователей (когда
    основа снова в строю и рычаг «по кнопке»), обновить, назад."""
    return ui.rows(
        ("↩️ Вернуть пользователей", RsvCB(action="back")) if can_return else None,
        ("🔄 Обновить", RsvCB(action="refresh")),
        _back())


def emergency(has_code: bool, peer_bot_url: str = "", peer_name: str = "") -> InlineKeyboardMarkup:
    from aiogram.types import InlineKeyboardButton
    rows = [[("🔑 Показать код", RsvCB(action="show_code")) if has_code else None,
             ("🔄 Сменить код", RsvCB(action="new_code")) if has_code else None]]
    markup = ui.rows(*rows, _back("rsv"))
    if peer_bot_url:
        # кнопка-ссылка на бот второй стороны: там свой экран и ввод кода
        markup.inline_keyboard.insert(len(markup.inline_keyboard) - 1,
                                      [InlineKeyboardButton(text=f"🆘 Открыть бот «{peer_name or 'резерв'}»"[:32], url=peer_bot_url)])
    return markup


def confirm_new_code() -> InlineKeyboardMarkup:
    return kc.confirm(RsvCB(action="emerg"), "🔄 Сменить", RsvCB(action="new_code_yes"))


def confirm_unpair() -> InlineKeyboardMarkup:
    return kc.confirm(SetCB(sec="rsv"), "🗑 Удалить", RsvCB(action="unpair_yes"))


def confirm_ready_off() -> InlineKeyboardMarkup:
    return kc.confirm(SetCB(sec="rsv"), "☑️ Выключить", RsvCB(action="ready_off_yes"))


def quorum_tie(reserve: str = "") -> InlineKeyboardMarkup:
    """Т-К5 (лидеру): переезд кнопкой (с именем второго сервера, как на карточке) и раздел."""
    label = f"🔀 Переезд на «{reserve}»" if reserve else "🔀 Переезд на резерв"
    return kc.append_hide_row(ui.rows((label, RsvCB(action="move")), ("🛟 Резерв", SetCB(sec="rsv"))))


def quorum_tie_cand() -> InlineKeyboardMarkup:
    """Т-К5б (кандидату): только раздел, над «Скрыть»."""
    return kc.append_hide_row(ui.rows(("🛟 Резерв", SetCB(sec="rsv"))))


def peer_code_offer() -> InlineKeyboardMarkup:
    """Т-РУ7б: «🔑 Ввести аварийный код» над «Скрыть»."""
    return kc.append_hide_row(ui.rows(("🔑 Ввести аварийный код", PeerCB(action="code"))))


def peer_code_cancel() -> InlineKeyboardMarkup:
    """Т-РН2: «⬅️ Отмена»."""
    return ui.rows(("⬅️ Отмена", PeerCB(action="code_cancel")))


def peer_admin(*, code_needed: bool, has_client: bool = False, in_card: bool = False) -> InlineKeyboardMarkup:
    """Экран роли админа второго сервера: обновить, ввод кода (только в Н4),
    «👤 Меню пользователя» (он ещё и пользователь здесь), скрыть; у общего админа экран —
    раздел его «🛟 Резерв»: вместо «Скрыть» — назад."""
    from awgbot.bot.keyboards.common import hide_only
    rows = ui.rows(("🔄 Обновить", PeerCB(action="refresh")),
                   ("🔑 Ввести аварийный код", PeerCB(action="code")) if code_needed else None,
                   ("👤 Меню пользователя", PeerCB(action="client")) if has_client and not in_card else None,
                   _back("rsv") if in_card else None)
    if in_card:
        return rows
    hide = hide_only().inline_keyboard
    return InlineKeyboardMarkup(inline_keyboard=rows.inline_keyboard + hide)
