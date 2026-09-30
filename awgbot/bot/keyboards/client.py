"""Меню клиента и гостя: устройства, карточка устройства, выдача конфигов, помощь, гайды, пауза."""

from __future__ import annotations

from urllib.parse import quote

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from awgbot.bot.callbacks import (
    BlockCB, CancelCB, DelDeviceCB, DeviceCB, FriendCB, GraceCB, GuideCB, HelpCB, Menu, PauseCB,
    RoutingCB)
from awgbot.bot import texts as _texts

from .common import (_btn_suffix, append_hide_row, _manual_block_button, confirm, issuable,
                     page_slice, page_nav)


ADD_FROM_DEVICES = 1          # DeviceCB(add, device_id=1): «➕ Устройство» из списка — отмена туда же


def _dot(dev) -> str:
    """Значок состояния для кнопки списка: ⛔ / ⏳ / 🟢 / ⚪."""
    return _texts.device_state(dev)


def issue_row(kb: InlineKeyboardBuilder, cb_cls, device_id: int = 0) -> None:
    """Ряд выдачи [🔗 Ссылка] [🔳 QR] [📄 Файл] — три кнопки одним рядом.
    device_id=0 — «выбери устройство» (при одном — сразу выдача)."""
    for text, action in (("🔗 Ссылка", "gen_link"), ("🔳 QR", "gen_qr"), ("📄 Файл", "gen_file")):
        kb.button(text=text, callback_data=cb_cls(action=action, device_id=device_id))


def _menu_issue_row(kb: InlineKeyboardBuilder) -> None:
    for text, action in (("🔗 Ссылка", "gen_link"), ("🔳 QR", "gen_qr"), ("📄 Файл", "gen_file")):
        kb.button(text=text, callback_data=Menu(action=action))


# ─────────────────────────────────────────────────────────────────────────────
# Главная клиента и гостя
# ─────────────────────────────────────────────────────────────────────────────

def client_main(has_devices: bool = True, routing_visible: bool = False,
                client_id: int = 0, can_add: bool = True) -> InlineKeyboardMarkup:
    """Главная клиента: ряд выдачи (когда есть что выдавать), устройства и
    добавление (пока есть место в лимите), РФ-доступ (только когда админ
    выдал) и подписка, помощь. Без РФ-доступа подписка и помощь — одним рядом."""
    kb = InlineKeyboardBuilder()
    rows = []
    if has_devices:
        _menu_issue_row(kb)
        rows.append(3)
        kb.button(text="📱 Устройства", callback_data=Menu(action="devices"))
        n = 1
        if can_add:
            kb.button(text="➕ Устройство", callback_data=DeviceCB(action="add"))
            n = 2
        rows.append(n)
    elif can_add:
        kb.button(text="➕ Устройство", callback_data=DeviceCB(action="add"))
        rows.append(1)
    if routing_visible:
        kb.button(text=f"🇷🇺 {_texts.ROUTING_NAME}", callback_data=RoutingCB(action="panel", ref=client_id))
        kb.button(text="💳 Подписка", callback_data=Menu(action="info"))
        kb.button(text="❓ Как подключить", callback_data=HelpCB(platform="root"))
        rows += [2, 1]
    else:
        kb.button(text="💳 Подписка", callback_data=Menu(action="info"))
        kb.button(text="❓ Как подключить", callback_data=HelpCB(platform="root"))
        rows.append(2)
    kb.adjust(*rows)
    return kb.as_markup()


def guest_main(*, routing_visible: bool = False, client_id: int = 0,
               has_devices: bool = True) -> InlineKeyboardMarkup:
    """Главная гостя: выдача, устройства, РФ-доступ (при фиче у владельца),
    помощь. Без устройств — только помощь."""
    kb = InlineKeyboardBuilder()
    if not has_devices:
        kb.button(text="❓ Как подключить", callback_data=FriendCB(action="help"))
        kb.adjust(1)
        return kb.as_markup()
    issue_row(kb, FriendCB)                    # device_id=0: одно — сразу, иначе выбор
    kb.button(text="📱 Устройства", callback_data=FriendCB(action="list"))
    rows = [3]
    if routing_visible:
        kb.button(text=f"🇷🇺 {_texts.ROUTING_NAME}", callback_data=RoutingCB(action="panel", ref=client_id))
        rows.append(2)
    else:
        rows.append(1)
    kb.button(text="❓ Как подключить", callback_data=FriendCB(action="help"))
    rows.append(1)
    kb.adjust(*rows)
    return kb.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Списки устройств
# ─────────────────────────────────────────────────────────────────────────────

def client_devices(devices, held=(), page: int = 0, render: str = "", *,
                   back=None, add: bool = True) -> InlineKeyboardMarkup:
    """Список своих устройств (переданное другу — с «[имя держателя]»);
    следом — чужие, которые профиль держит, с пометкой «от профиля …».
    Значок — состояние (⛔ ⏳ 🟢 ⚪). add — есть место в лимите."""
    kb = InlineKeyboardBuilder()
    rows = [(d, False) for d in devices] + [(d, True) for d in held]
    chunk, page, prev, nxt = page_slice(rows, page, static=1)
    for _i, (d, is_held) in chunk:
        if is_held:
            label = f"{_dot(d)} {d.name} · от профиля {_texts.owner_name(d)}"
        else:
            lent = f" [{_texts.holder_name(d)}]" if getattr(d, "is_lent", False) else ""
            label = f"{_dot(d)} {d.name}{_btn_suffix(d)}{lent}"
        kb.button(text=label, callback_data=DeviceCB(action="open", device_id=d.id))
    nav = page_nav(kb, "devices", 0, page, prev, nxt, render or Menu(action="devices").pack())
    tail = 0
    if add:
        kb.button(text="➕ Устройство", callback_data=DeviceCB(action="add", device_id=ADD_FROM_DEVICES))
        tail += 1
    kb.button(text="⬅️ Назад", callback_data=back or Menu(action="main").pack())
    tail += 1
    kb.adjust(*([1] * len(chunk)), *([nav] if nav else []), tail)
    return kb.as_markup()


def guest_devices(devices, page: int = 0) -> InlineKeyboardMarkup:
    """«📱 Устройства» гостя: список, назад — на главный экран гостя."""
    kb = InlineKeyboardBuilder()
    chunk, page, prev, nxt = page_slice(devices, page, static=1)
    for _i, d in chunk:
        kb.button(text=f"{_dot(d)} {d.name}", callback_data=FriendCB(action="open", device_id=d.id))
    nav = page_nav(kb, "gdevices", 0, page, prev, nxt, FriendCB(action="list").pack())
    kb.button(text="⬅️ Назад", callback_data=FriendCB(action="refresh"))
    kb.adjust(*([1] * len(chunk)), *([nav] if nav else []), 1)
    return kb.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Карточки устройств
# ─────────────────────────────────────────────────────────────────────────────

def device_actions(dev, *, is_admin: bool, back_target: str) -> InlineKeyboardMarkup:
    """Карточка устройства — для любого пути входа. Ряд выдачи — только у
    созданных ботом (у пира без приватного ключа выдавать нечего); затем имя и
    лимит, передача (владельцу — другу, админу — в другой профиль) и блок,
    удаление и назад. Удаление — всегда через подтверждение."""
    kb = InlineKeyboardBuilder()
    rows = []
    if dev.is_managed:
        issue_row(kb, DeviceCB, dev.id)
        rows.append(3)
    kb.button(text="✏️ Имя", callback_data=DeviceCB(action="edit_name", device_id=dev.id))
    kb.button(text="✏️ Лимит", callback_data=DeviceCB(action="edit_traffic", device_id=dev.id))
    rows.append(2)
    mid = 0
    fstatus = dev.friend_status
    if not is_admin:
        if dev.is_managed and fstatus is None:
            kb.button(text="👤 Другу", callback_data=DeviceCB(action="transfer", device_id=dev.id))
            mid += 1
        elif fstatus == "pending":
            kb.button(text="🔁 Приглашение", callback_data=DeviceCB(action="reinvite", device_id=dev.id))
            mid += 1
    else:
        kb.button(text="🔀 Передать", callback_data=DeviceCB(action="reassign", device_id=dev.id))
        mid += 1
    bt, bcb = _manual_block_button("dev", dev.id, int(dev.block_reason), for_admin=is_admin)
    kb.button(text=bt, callback_data=bcb)
    rows.append(mid + 1)
    kb.button(text="🗑 Удалить", callback_data=DelDeviceCB(device_id=dev.id, stage="ask"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_target))
    kb.adjust(*rows, 2)
    return kb.as_markup()


def lent_out_device_actions(dev, back_target: str) -> InlineKeyboardMarkup:
    """Своё переданное — у владельца: имя, лимит, удаление; остальным
    управляет держатель."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Имя", callback_data=DeviceCB(action="edit_name", device_id=dev.id))
    kb.button(text="✏️ Лимит", callback_data=DeviceCB(action="edit_traffic", device_id=dev.id))
    kb.button(text="🗑 Удалить", callback_data=DelDeviceCB(device_id=dev.id, stage="ask"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_target))
    kb.adjust(2, 2)
    return kb.as_markup()


def held_device_actions(dev, back_target: str, *, cb_cls=None) -> InlineKeyboardMarkup:
    """Удерживаемое (от друга) у держателя — гостя или клиента: выдача, блок,
    удаление. cb_cls — класс колбэков выдачи (DeviceCB у клиента, FriendCB у
    гостя)."""
    cb_cls = cb_cls or DeviceCB
    kb = InlineKeyboardBuilder()
    issue_row(kb, cb_cls, dev.id)
    bt, bcb = _manual_block_button("dev", dev.id, int(dev.block_reason), for_admin=False)
    kb.button(text=bt, callback_data=bcb)
    kb.button(text="🗑 Удалить", callback_data=DelDeviceCB(device_id=dev.id, stage="ask"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_target))
    kb.adjust(3, 2, 1)
    return kb.as_markup()


def block_device_confirm(device_id: int, *, guest: bool = False) -> InlineKeyboardMarkup:
    back = (FriendCB(action="open", device_id=device_id) if guest
            else DeviceCB(action="open", device_id=device_id))
    return confirm(back, "🛑 Заблокировать",
                   BlockCB(target="dev", action="block", ref=device_id, kind="user"))


def confirm_delete_device(device_id: int, only: bool = False, *, guest: bool = False) -> InlineKeyboardMarkup:
    """«⬅️ Отмена» — к карточке этого устройства; «🗑 Удалить» — красным."""
    back = (FriendCB(action="open", device_id=device_id) if guest
            else DeviceCB(action="open", device_id=device_id))
    return confirm(back, "🗑 Удалить", DelDeviceCB(device_id=device_id, stage="confirm"))


def confirm_transfer(device_id: int) -> InlineKeyboardMarkup:
    return confirm(DeviceCB(action="open", device_id=device_id), "👤 Передать",
                   DeviceCB(action="transfer_yes", device_id=device_id), danger=False)


def unmanaged_device_dialog(device_id: int) -> InlineKeyboardMarkup:
    """Клик по устройству без ключа в выборе под ссылку: удалить / назад."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Удалить", callback_data=DelDeviceCB(device_id=device_id, stage="ask"))
    kb.button(text="⬅️ Назад", callback_data=Menu(action="main"))
    kb.adjust(2)
    return kb.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Выбор устройства под выдачу
# ─────────────────────────────────────────────────────────────────────────────

PICK_DEVICE_PROMPT = {"gen_link": _texts.pick_device_header("link"),
                      "gen_qr": _texts.pick_device_header("qr"),
                      "gen_file": _texts.pick_device_header("file")}
GEN_ACTIONS = frozenset(PICK_DEVICE_PROMPT)       # DeviceCB/FriendCB: три вида выдачи


def gen_kind(action: str) -> str:
    """«gen_link» → «link»: вид выдачи для send_device_config."""
    return action[len("gen_"):]


def pick_device(devices, action: str, back_cb: str = None, page: int = 0,
                render: str = "", ref: int = 0) -> InlineKeyboardMarkup:
    """action: gen_link | gen_file | gen_qr — выбор устройства со значком
    состояния. Устройства без ключа — с суффиксом: клик ведёт в диалог
    «удали», а не в ошибку. Шлюз не предлагается: его конфиг едет только в
    конфигурации шлюза."""
    kb = InlineKeyboardBuilder()
    chunk, page, prev, nxt = page_slice(issuable(devices), page, static=1)
    for _i, d in chunk:
        kb.button(text=f"{_dot(d)} {d.name}{_btn_suffix(d)}",
                  callback_data=DeviceCB(action=action, device_id=d.id))
    nav = page_nav(kb, "pick", ref, page, prev, nxt, render or Menu(action=action).pack())
    kb.adjust(*([1] * len(chunk)), *([nav] if nav else []))
    kb.row(InlineKeyboardButton(
        text="⬅️ Назад", callback_data=back_cb or Menu(action="main").pack()))
    return kb.as_markup()


def guest_pick_device(devices, action: str, page: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    chunk, page, prev, nxt = page_slice(devices, page, static=1)
    for _i, d in chunk:
        kb.button(text=f"{_dot(d)} {d.name}", callback_data=FriendCB(action=action, device_id=d.id))
    nav = page_nav(kb, "gpick", 0, page, prev, nxt, FriendCB(action=action).pack())
    kb.button(text="⬅️ Назад", callback_data=FriendCB(action="refresh"))
    kb.adjust(*([1] * len(chunk)), *([nav] if nav else []), 1)
    return kb.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Добавление устройства, лимит, приглашение
# ─────────────────────────────────────────────────────────────────────────────

def add_device_kb(*, for_friend: bool, ctx_kind: str = "main") -> InlineKeyboardMarkup:
    """Под приглашением ввода имени: переключатель «для кого» и отмена на
    экран, откуда пришли."""
    kb = InlineKeyboardBuilder()
    if for_friend:
        kb.button(text="📱 Это для меня", callback_data=DeviceCB(action="add_self"))
    else:
        kb.button(text="👤 Это для друга", callback_data=DeviceCB(action="add_friend"))
    kb.button(text="✖️ Отмена", callback_data=CancelCB(kind=ctx_kind))
    kb.adjust(1, 1)
    return kb.as_markup()


def device_created_kb(device_id: int) -> InlineKeyboardMarkup:
    """После создания своего устройства: сразу выдача, помощь, в меню."""
    kb = InlineKeyboardBuilder()
    issue_row(kb, DeviceCB, device_id)
    kb.button(text="❓ Как подключить", callback_data=HelpCB(platform="root"))
    kb.button(text="⬅️ В меню", callback_data=Menu(action="main"))
    kb.adjust(3, 2)
    return kb.as_markup()


def invite_kb(plain_text: str, link: str) -> InlineKeyboardMarkup:
    """Под приглашением другу: «📤 Отправить» — выбор чата (t.me/share) с тем
    же текстом, «📋 Скопировать» — текст в буфер."""
    share = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(plain_text, safe='')}"
    row = [InlineKeyboardButton(text="📤 Отправить", url=share),
           InlineKeyboardButton(text="📋 Скопировать", copy_text=CopyTextButton(text=plain_text[:256]))]
    return InlineKeyboardMarkup(inline_keyboard=[row])


# ─────────────────────────────────────────────────────────────────────────────
# Помощь и гайды
# ─────────────────────────────────────────────────────────────────────────────

def help_menu(is_initial: bool = False, *, guest: bool = False) -> InlineKeyboardMarkup:
    """Платформы по две в ряд. is_initial — первый гайд сразу после
    активации: вместо «В меню» — «✅ Настрою сам». guest — тот же выход
    «✅ Настрою сам» на главную гостя (его помощь всегда «первая»: сам он
    устройств не заводит)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🍎 iPhone / iPad", callback_data=HelpCB(platform="apple"))
    kb.button(text="🤖 Android", callback_data=HelpCB(platform="android"))
    kb.button(text="🪟 Windows", callback_data=HelpCB(platform="windows"))
    kb.button(text="🍏 Mac", callback_data=HelpCB(platform="mac"))
    if is_initial:
        kb.button(text="✅ Настрою сам", callback_data=HelpCB(platform="skip"))
    elif guest:
        kb.button(text="✅ Настрою сам", callback_data=FriendCB(action="refresh"))
    else:
        kb.button(text="⬅️ В меню", callback_data=Menu(action="main"))
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def friend_finisher() -> InlineKeyboardMarkup:
    """«⬅️ В меню» под содержимым гостя — возврат на его главный экран."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В меню", callback_data=FriendCB(action="refresh"))
    return kb.as_markup()


def _menu_button(guest: bool) -> InlineKeyboardButton:
    if guest:
        return InlineKeyboardButton(text="⬅️ В меню", callback_data=FriendCB(action="refresh").pack())
    return InlineKeyboardButton(text="⬅️ В меню", callback_data=Menu(action="main").pack())


def guide_nav(guide: str, step: int, last: int, *, next_guide: str = None,
              apple_connect_end: bool = False, guest: bool = False) -> InlineKeyboardMarkup:
    """Кнопки под шагом гайда: Назад / Далее (или переход к подключению),
    затем «В меню». last — индекс последнего шага."""
    kb = InlineKeyboardBuilder()
    row = 0
    if step > 0:
        kb.button(text="⬅️ Назад", callback_data=GuideCB(guide=guide, step=step - 1))
        row += 1
    if step < last:
        kb.button(text="Далее ➡️", callback_data=GuideCB(guide=guide, step=step + 1))
        row += 1
    elif next_guide:
        kb.button(text="📶 Подключение", callback_data=GuideCB(guide=next_guide, step=0))
        row += 1
    if apple_connect_end:
        kb.button(text="🎛 VPN в шторку", callback_data=GuideCB(guide="toggle", step=0))
    kb.row(_menu_button(guest))
    if apple_connect_end:
        kb.adjust(row if row else 1, 1, 1)
    else:
        kb.adjust(row if row else 1, 1)
    return kb.as_markup()


def guide_connect_method(device_id: int, guide: str, *, guest: bool = False) -> InlineKeyboardMarkup:
    """Шаг «Настраиваем подключение»: ряд выдачи для выбранного устройства."""
    kb = InlineKeyboardBuilder()
    for text, kind in (("🔗 Ссылка", "link"), ("🔳 QR", "qr"), ("📄 Файл", "file")):
        kb.button(text=text, callback_data=GuideCB(guide=guide, step=1, dev=device_id, kind=kind))
    kb.button(text="⬅️ Назад", callback_data=GuideCB(guide=guide, step=0))
    kb.row(_menu_button(guest))
    kb.adjust(3, 1, 1)
    return kb.as_markup()


def guide_connect_done(guide: str, device_id: int, *, apple_end: bool,
                       guest: bool = False) -> InlineKeyboardMarkup:
    """Шаг «Подключаемся»: назад к выбору способа; на Apple — гайд про шторку."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=GuideCB(guide=guide, step=1, dev=device_id))
    if apple_end:
        kb.button(text="🎛 VPN в шторку", callback_data=GuideCB(guide="toggle", step=0))
    kb.row(_menu_button(guest))
    kb.adjust(1, 1, 1)
    return kb.as_markup()


def guide_connect_devices(devices, slots, guide: str = "connect", page: int = 0,
                          *, guest: bool = False) -> InlineKeyboardMarkup:
    """Шаг 0 подключения: добавить (клиенту, пока есть место) + устройства.
    Кнопки сами ведут дальше — отдельной «Далее» нет."""
    used, limit = slots
    kb = InlineKeyboardBuilder()
    can_add = (not guest) and (limit == 0 or used < limit)
    if can_add:
        kb.button(text="➕ Устройство", callback_data=GuideCB(guide=guide, step=-1))
    chunk, page, prev, nxt = page_slice(issuable(devices), page, static=2 if can_add else 1)   # ряды
    for _i, d in chunk:
        kb.button(text=f"🔗 {d.name}", callback_data=DeviceCB(action="gen_guide", device_id=d.id))
    nav = page_nav(kb, "guidedev", 0, page, prev, nxt, GuideCB(guide=guide, step=0).pack())
    kb.row(_menu_button(guest))
    kb.adjust(*([1] if can_add else []), *([1] * len(chunk)), *([nav] if nav else []), 1)
    return kb.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Уведомления клиента
# ─────────────────────────────────────────────────────────────────────────────

def added_by_admin(device_id: int) -> InlineKeyboardMarkup:
    """«Устройство добавлено администратором»: ряд выдачи, помощь, «Скрыть»."""
    kb = InlineKeyboardBuilder()
    issue_row(kb, DeviceCB, device_id)
    kb.button(text="❓ Как подключить", callback_data=HelpCB(platform="root"))
    kb.adjust(3, 1)
    return append_hide_row(kb)


def grace_offer(client_id: int, days: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Продли чуток? 🙏 (+{days} дн.)",
              callback_data=GraceCB(action="take", ref=client_id))
    kb.adjust(1)
    return append_hide_row(kb)


# ─────────────────────────────────────────────────────────────────────────────
# Подписка и пауза
# ─────────────────────────────────────────────────────────────────────────────

def subscription_kb(client_id: int, *, paused_user: bool, can_pause: bool) -> InlineKeyboardMarkup:
    """Под «💳 Подписка»: пауза (когда есть дни), снятие своей паузы — сразу,
    без вопроса; администраторскую снимает админ."""
    kb = InlineKeyboardBuilder()
    n = 1
    if paused_user:
        kb.button(text="▶️ Снять паузу", callback_data=PauseCB(action="resume", ref=client_id))
        n = 2
    elif can_pause:
        kb.button(text="⏸️ Пауза", callback_data=PauseCB(action="ask", ref=client_id))
        n = 2
    kb.button(text="⬅️ Назад", callback_data=Menu(action="main"))
    kb.adjust(n)
    return kb.as_markup()


def pause_kb(client_id: int, available: int) -> InlineKeyboardMarkup:
    """Пресеты дней = подтверждение: 7 / 14 (не выше доступного) и весь
    остаток, если он не совпал с пресетом; «✏️ Другое» — ввод."""
    kb = InlineKeyboardBuilder()
    shown = [p for p in (7, 14) if p <= available]
    if available not in shown:
        shown.append(available)
    for preset in shown:
        kb.button(text=f"{preset} дн.", callback_data=PauseCB(action="pick", ref=client_id, days=preset))
    other = available >= 2
    if other:
        kb.button(text="✏️ Другое", callback_data=PauseCB(action="other", ref=client_id))
    kb.button(text="⬅️ Отмена", callback_data=PauseCB(action="cancel", ref=client_id))
    n = len(shown)
    kb.adjust(*([2] * (n // 2) + ([1] if n % 2 else [])), 2 if other else 1)
    return kb.as_markup()
