"""Меню клиента и гостя: устройства, карточка устройства, выдача конфигов, помощь, гайды, пауза."""

from __future__ import annotations

from urllib.parse import quote

from aiogram.types import InlineKeyboardMarkup
from awgbot.bot import ui
from awgbot.bot.callbacks import (
    BlockCB, CancelCB, DelDeviceCB, DeviceCB, FriendCB, GraceCB, GuideCB, HelpCB, Menu, PauseCB,
    RoutingCB)
from awgbot.bot import texts as _texts

from .common import (_btn_suffix, append_hide_row, _manual_block_button, confirm, issuable)


ADD_FROM_DEVICES = 1          # DeviceCB(add, device_id=1): «➕ Устройство» из списка — отмена туда же
_ISSUE = (("🔗 Ссылка", "gen_link"), ("🔳 QR", "gen_qr"), ("📄 Файл", "gen_file"))


def _dot(dev) -> str:
    """Значок состояния для кнопки списка: ⛔ / ⏳ / 🟢 / ⚪."""
    return _texts.device_state(dev)


def issue_row(cb_cls, device_id: int = 0) -> list:
    """Ряд выдачи [🔗 Ссылка] [🔳 QR] [📄 Файл] — три кнопки одним рядом.
    device_id=0 — «выбери устройство» (при одном — сразу выдача)."""
    return [(text, cb_cls(action=action, device_id=device_id)) for text, action in _ISSUE]


def menu_issue_row() -> list:
    """Тот же ряд выдачи с главной: колбэки меню (при одном устройстве — сразу
    выдача, иначе выбор)."""
    return [(text, Menu(action=action)) for text, action in _ISSUE]


# ─────────────────────────────────────────────────────────────────────────────
# Главная клиента и гостя
# ─────────────────────────────────────────────────────────────────────────────

def client_main(has_devices: bool = True, routing_visible: bool = False,
                client_id: int = 0, can_add: bool = True) -> InlineKeyboardMarkup:
    """Главная клиента: ряд выдачи (когда есть что выдавать), устройства и
    добавление (пока есть место в лимите), РФ-доступ (только когда админ
    выдал) и подписка, помощь. Без РФ-доступа подписка и помощь — одним рядом."""
    add = ("➕ Устройство", DeviceCB(action="add")) if can_add else None
    sub = ("💳 Подписка", Menu(action="info"))
    help_ = ("❓ Как подключить", HelpCB(platform="root"))
    return ui.rows(
        menu_issue_row() if has_devices else None,
        [("📱 Устройства", Menu(action="devices")), add] if has_devices else [add],
        [(f"🇷🇺 {_texts.ROUTING_NAME}", RoutingCB(action="panel", ref=client_id)), sub] if routing_visible else [sub, help_],
        help_ if routing_visible else None)


def guest_main(*, routing_visible: bool = False, client_id: int = 0,
               has_devices: bool = True) -> InlineKeyboardMarkup:
    """Главная гостя: выдача, устройства, РФ-доступ (при фиче у владельца),
    помощь. Без устройств — только помощь."""
    help_ = ("❓ Как подключить", FriendCB(action="help"))
    if not has_devices:
        return ui.rows(help_)
    return ui.rows(
        issue_row(FriendCB),                    # device_id=0: одно — сразу, иначе выбор
        [("📱 Устройства", FriendCB(action="list")),
         (f"🇷🇺 {_texts.ROUTING_NAME}", RoutingCB(action="panel", ref=client_id)) if routing_visible else None],
        help_)


# ─────────────────────────────────────────────────────────────────────────────
# Списки устройств
# ─────────────────────────────────────────────────────────────────────────────

def client_devices(devices, held=(), page: int = 0, render: str = "", *,
                   back=None, add: bool = True) -> InlineKeyboardMarkup:
    """Список своих устройств (переданное другу — с «[имя держателя]»);
    следом — чужие, которые профиль держит, с пометкой «от профиля …».
    Значок — состояние (⛔ ⏳ 🟢 ⚪). add — есть место в лимите."""
    def _entry(_i, item):
        d, is_held = item
        if is_held:
            label = f"{_dot(d)} {d.name} · от профиля {_texts.owner_name(d)}"
        else:
            lent = f" [{_texts.holder_name(d)}]" if getattr(d, "is_lent", False) else ""
            label = f"{_dot(d)} {d.name}{_btn_suffix(d)}{lent}"
        return (label, DeviceCB(action="open", device_id=d.id))
    return ui.rows(
        *ui.paged([(d, False) for d in devices] + [(d, True) for d in held], page, static=1,
                  screen="devices", ref=0, back=render or Menu(action="devices").pack(), button=_entry),
        [("➕ Устройство", DeviceCB(action="add", device_id=ADD_FROM_DEVICES)) if add else None,
         ui.back(back or Menu(action="main").pack())])


def guest_devices(devices, page: int = 0) -> InlineKeyboardMarkup:
    """«📱 Устройства» гостя: список, назад — на главный экран гостя."""
    return ui.rows(
        *ui.paged(devices, page, static=1, screen="gdevices", ref=0, back=FriendCB(action="list").pack(),
                  button=lambda _i, d: (f"{_dot(d)} {d.name}", FriendCB(action="open", device_id=d.id))),
        ui.back(FriendCB(action="refresh")))


# ─────────────────────────────────────────────────────────────────────────────
# Карточки устройств
# ─────────────────────────────────────────────────────────────────────────────

def device_actions(dev, *, is_admin: bool, back_target: str) -> InlineKeyboardMarkup:
    """Карточка устройства — для любого пути входа. Ряд выдачи — только у
    созданных ботом (у пира без приватного ключа выдавать нечего); затем имя и
    лимит, передача (владельцу — другу, админу — в другой профиль) и блок,
    удаление и назад. Удаление — всегда через подтверждение."""
    fstatus = dev.friend_status
    if not is_admin:
        if dev.is_managed and fstatus is None:
            pass_on = ("👤 Другу", DeviceCB(action="transfer", device_id=dev.id))
        elif fstatus == "pending":
            pass_on = ("🔁 Приглашение", DeviceCB(action="reinvite", device_id=dev.id))
        else:
            pass_on = None
    else:
        pass_on = ("🔀 Передать", DeviceCB(action="reassign", device_id=dev.id))
    return ui.rows(
        issue_row(DeviceCB, dev.id) if dev.is_managed else None,
        [("✏️ Имя", DeviceCB(action="edit_name", device_id=dev.id)),
         ("✏️ Лимит", DeviceCB(action="edit_traffic", device_id=dev.id))],
        [pass_on, _manual_block_button("dev", dev.id, int(dev.block_reason), for_admin=is_admin)],
        [("🗑 Удалить", DelDeviceCB(device_id=dev.id, stage="ask")), ui.back(back_target)])


def lent_out_device_actions(dev, back_target: str) -> InlineKeyboardMarkup:
    """Своё переданное — у владельца: имя, лимит, удаление; остальным
    управляет держатель."""
    return ui.rows(
        [("✏️ Имя", DeviceCB(action="edit_name", device_id=dev.id)),
         ("✏️ Лимит", DeviceCB(action="edit_traffic", device_id=dev.id))],
        [("🗑 Удалить", DelDeviceCB(device_id=dev.id, stage="ask")), ui.back(back_target)])


def held_device_actions(dev, back_target: str, *, cb_cls=None) -> InlineKeyboardMarkup:
    """Удерживаемое (от друга) у держателя — гостя или клиента: выдача, блок,
    удаление. cb_cls — класс колбэков выдачи (DeviceCB у клиента, FriendCB у
    гостя)."""
    return ui.rows(
        issue_row(cb_cls or DeviceCB, dev.id),
        [_manual_block_button("dev", dev.id, int(dev.block_reason), for_admin=False),
         ("🗑 Удалить", DelDeviceCB(device_id=dev.id, stage="ask"))],
        ui.back(back_target))


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
    return ui.rows([("🗑 Удалить", DelDeviceCB(device_id=device_id, stage="ask")), ui.back(Menu(action="main"))])


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
    return ui.rows(
        *ui.paged(issuable(devices), page, static=1, screen="pick", ref=ref,
                  back=render or Menu(action=action).pack(),
                  button=lambda _i, d: (f"{_dot(d)} {d.name}{_btn_suffix(d)}", DeviceCB(action=action, device_id=d.id))),
        ui.back(back_cb or Menu(action="main").pack()))


def guest_pick_device(devices, action: str, page: int = 0) -> InlineKeyboardMarkup:
    return ui.rows(
        *ui.paged(devices, page, static=1, screen="gpick", ref=0, back=FriendCB(action=action).pack(),
                  button=lambda _i, d: (f"{_dot(d)} {d.name}", FriendCB(action=action, device_id=d.id))),
        ui.back(FriendCB(action="refresh")))


# ─────────────────────────────────────────────────────────────────────────────
# Добавление устройства, лимит, приглашение
# ─────────────────────────────────────────────────────────────────────────────

def add_device_kb(*, for_friend: bool, ctx_kind: str = "main") -> InlineKeyboardMarkup:
    """Под приглашением ввода имени: переключатель «для кого» и отмена на
    экран, откуда пришли."""
    return ui.rows(
        ("📱 Это для меня", DeviceCB(action="add_self")) if for_friend else ("👤 Это для друга", DeviceCB(action="add_friend")),
        ("✖️ Отмена", CancelCB(kind=ctx_kind)))


def device_created_kb(device_id: int) -> InlineKeyboardMarkup:
    """После создания своего устройства: сразу выдача, помощь, в меню."""
    return ui.rows(issue_row(DeviceCB, device_id),
                   [("❓ Как подключить", HelpCB(platform="root")), ui.to_menu(Menu(action="main"))])


def invite_kb(plain_text: str, link: str) -> InlineKeyboardMarkup:
    """Под приглашением другу: «📤 Отправить» — выбор чата (t.me/share) с тем
    же текстом, «📋 Скопировать» — текст в буфер."""
    share = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(plain_text, safe='')}"
    return ui.rows([ui.btn("📤 Отправить", url=share), ui.btn("📋 Скопировать", copy=plain_text[:256])])


# ─────────────────────────────────────────────────────────────────────────────
# Помощь и гайды
# ─────────────────────────────────────────────────────────────────────────────

def help_menu(is_initial: bool = False, *, guest: bool = False) -> InlineKeyboardMarkup:
    """Платформы по две в ряд. is_initial — первый гайд сразу после
    активации: вместо «В меню» — «✅ Настрою сам». guest — тот же выход
    «✅ Настрою сам» на главную гостя (его помощь всегда «первая»: сам он
    устройств не заводит)."""
    if is_initial:
        exit_ = ("✅ Настрою сам", HelpCB(platform="skip"))
    elif guest:
        exit_ = ("✅ Настрою сам", FriendCB(action="refresh"))
    else:
        exit_ = ui.to_menu(Menu(action="main"))
    return ui.rows(
        [("🍎 iPhone / iPad", HelpCB(platform="apple")), ("🤖 Android", HelpCB(platform="android"))],
        [("🪟 Windows", HelpCB(platform="windows")), ("🍏 Mac", HelpCB(platform="mac"))],
        exit_)


def friend_finisher() -> InlineKeyboardMarkup:
    """«⬅️ В меню» под содержимым гостя — возврат на его главный экран."""
    return ui.rows(ui.to_menu(FriendCB(action="refresh")))


def _menu_button(guest: bool) -> tuple:
    return ui.to_menu(FriendCB(action="refresh") if guest else Menu(action="main"))


def guide_nav(guide: str, step: int, last: int, *, next_guide: str = None,
              apple_connect_end: bool = False, guest: bool = False) -> InlineKeyboardMarkup:
    """Кнопки под шагом гайда: Назад / Далее (или переход к подключению),
    затем «В меню». last — индекс последнего шага."""
    if step < last:
        forward = ("Далее ➡️", GuideCB(guide=guide, step=step + 1))
    elif next_guide:
        forward = ("📶 Подключение", GuideCB(guide=next_guide, step=0))
    else:
        forward = None
    return ui.rows(
        [ui.back(GuideCB(guide=guide, step=step - 1)) if step > 0 else None, forward],
        ("🎛 VPN в шторку", GuideCB(guide="toggle", step=0)) if apple_connect_end else None,
        _menu_button(guest))


def guide_connect_method(device_id: int, guide: str, *, guest: bool = False) -> InlineKeyboardMarkup:
    """Шаг «Настраиваем подключение»: ряд выдачи для выбранного устройства."""
    return ui.rows(
        [(text, GuideCB(guide=guide, step=1, dev=device_id, kind=kind))
         for text, kind in (("🔗 Ссылка", "link"), ("🔳 QR", "qr"), ("📄 Файл", "file"))],
        ui.back(GuideCB(guide=guide, step=0)),
        _menu_button(guest))


def guide_connect_done(guide: str, device_id: int, *, apple_end: bool,
                       guest: bool = False) -> InlineKeyboardMarkup:
    """Шаг «Подключаемся»: назад к выбору способа; на Apple — гайд про шторку."""
    return ui.rows(
        ui.back(GuideCB(guide=guide, step=1, dev=device_id)),
        ("🎛 VPN в шторку", GuideCB(guide="toggle", step=0)) if apple_end else None,
        _menu_button(guest))


def guide_connect_devices(devices, slots, guide: str = "connect", page: int = 0,
                          *, guest: bool = False) -> InlineKeyboardMarkup:
    """Шаг 0 подключения: добавить (клиенту, пока есть место) + устройства.
    Кнопки сами ведут дальше — отдельной «Далее» нет."""
    used, limit = slots
    can_add = (not guest) and (limit == 0 or used < limit)
    return ui.rows(
        ("➕ Устройство", GuideCB(guide=guide, step=-1)) if can_add else None,
        *ui.paged(issuable(devices), page, static=2 if can_add else 1, screen="guidedev", ref=0,
                  back=GuideCB(guide=guide, step=0).pack(),
                  button=lambda _i, d: (f"🔗 {d.name}", DeviceCB(action="gen_guide", device_id=d.id))),
        _menu_button(guest))


# ─────────────────────────────────────────────────────────────────────────────
# Уведомления клиента
# ─────────────────────────────────────────────────────────────────────────────

def added_by_admin(device_id: int) -> InlineKeyboardMarkup:
    """«Устройство добавлено администратором»: ряд выдачи, помощь, «Скрыть»."""
    return append_hide_row(ui.rows(issue_row(DeviceCB, device_id), ("❓ Как подключить", HelpCB(platform="root"))))


def grace_offer(client_id: int, days: int) -> InlineKeyboardMarkup:
    return append_hide_row(ui.rows((f"Продли чуток? 🙏 (+{days} дн.)", GraceCB(action="take", ref=client_id))))


# ─────────────────────────────────────────────────────────────────────────────
# Подписка и пауза
# ─────────────────────────────────────────────────────────────────────────────

def subscription_kb(client_id: int, *, paused_user: bool, can_pause: bool) -> InlineKeyboardMarkup:
    """Под «💳 Подписка»: пауза (когда есть дни), снятие своей паузы — сразу,
    без вопроса; администраторскую снимает админ."""
    if paused_user:
        pause = ("▶️ Снять паузу", PauseCB(action="resume", ref=client_id))
    elif can_pause:
        pause = ("⏸️ Пауза", PauseCB(action="ask", ref=client_id))
    else:
        pause = None
    return ui.rows([pause, ui.back(Menu(action="main"))])


def pause_kb(client_id: int, available: int) -> InlineKeyboardMarkup:
    """Пресеты дней = подтверждение: 7 / 14 (не выше доступного) и весь
    остаток, если он не совпал с пресетом; «✏️ Другое» — ввод."""
    shown = [p for p in (7, 14) if p <= available]
    if available not in shown:
        shown.append(available)
    return ui.rows(
        *ui.grid([(f"{preset} дн.", PauseCB(action="pick", ref=client_id, days=preset)) for preset in shown], 2),
        [("✏️ Другое", PauseCB(action="other", ref=client_id)) if available >= 2 else None,
         ("⬅️ Отмена", PauseCB(action="cancel", ref=client_id))])
