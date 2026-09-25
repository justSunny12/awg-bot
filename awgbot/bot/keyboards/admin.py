"""Меню администратора: главное меню, профили, устройства без клиента, списки."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from awgbot.core import blocks as _blocks
from awgbot.core.enums import ActivationStatus
from awgbot.bot.callbacks import (
    AdminSelfCB, ClientCB, ConfirmCB, DeviceCB, Menu, ReassignCB, RoutingCB, SetCB,
    BroadcastCB)

from .common import _chk, _btn_suffix, _dev_emoji, _manual_block_button, page_slice, page_nav


# ─────────────────────────────────────────────────────────────────────────────
# Админские меню
# ─────────────────────────────────────────────────────────────────────────────

def admin_add_device_choice() -> InlineKeyboardMarkup:
    """Кнопка «Добавить устройство» в главном меню админа — сначала спрашиваем,
    кому: себе (админ всегда безлимитен по устройствам, гейт не нужен) или
    конкретному клиенту."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Себе", callback_data=AdminSelfCB(action="add"))
    kb.button(text="👤 В другой профиль", callback_data=Menu(action="add_device_pick"))
    kb.button(text="⬅️ Назад", callback_data=Menu(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def pick_client_for_add_device(clients, page: int = 0) -> InlineKeyboardMarkup:
    """Список клиентов для «Добавить устройство → другому клиенту»."""
    kb = InlineKeyboardBuilder()
    chunk, page, prev, nxt = page_slice(clients, page, static=1)
    for _i, c in chunk:
        kb.button(text=c.name, callback_data=ClientCB(action="add_device", client_id=c.id))
    nav = page_nav(kb, "addpick", 0, page, prev, nxt, Menu(action="add_device_pick").pack())
    kb.button(text="⬅️ Назад", callback_data=Menu(action="main"))
    kb.adjust(*([1] * len(chunk)), *([nav] if nav else []), 1)
    return kb.as_markup()


def admin_main(unassigned_count: int, self_has_devices: bool = False,
               routing_visible: bool = False, routing_on: bool = False,
               self_client_id: int = 0, self_can_issue: bool | None = None) -> InlineKeyboardMarkup:
    """Главное меню админа. Личный блок (он тоже пользователь VPN) сверху,
    затем управление клиентской базой. «Добавить устройство» ведёт в диалог
    выбора (себе/другому клиенту) — там же гейт по личному лимиту, а не тут:
    другому клиенту добавлять можно и при исчерпанном личном лимите.
    self_can_issue — есть ли устройство, которому можно выдать ссылку/QR/файл
    (шлюз в «Моих устройствах» есть, а ссылки у него нет); по умолчанию — как
    self_has_devices."""
    if self_can_issue is None:
        self_can_issue = self_has_devices
    kb = InlineKeyboardBuilder()
    pattern: list[int] = []
    kb.button(text="➕ Добавить устройство", callback_data=Menu(action="add_device_choice"))
    pattern.append(1)
    if self_has_devices:
        kb.button(text="📱 Мои устройства", callback_data=AdminSelfCB(action="devices"))
        pattern.append(1)
        if routing_visible:
            # админ — такой же пользователь VPN, и режим ему нужен там же, где
            # остальным: рядом со своими устройствами, а не в админских разделах
            kb.button(text=f"{_chk(routing_on)} Доступ к РФ-сервисам",
                      callback_data=RoutingCB(action="panel", ref=self_client_id))
            pattern.append(1)
        if self_can_issue:
            kb.button(text="🔗 Ссылка", callback_data=AdminSelfCB(action="gen_link"))
            kb.button(text="🔳 QR-код", callback_data=AdminSelfCB(action="gen_qr"))
            kb.button(text="📄 Файл", callback_data=AdminSelfCB(action="gen_file"))
            pattern.append(3)
    if unassigned_count > 0:
        kb.button(text=f"📦 Устройства без профиля ({unassigned_count})",
                  callback_data=Menu(action="unassigned"))
        pattern.append(1)
    kb.button(text="👥 Профили", callback_data=Menu(action="clients"))
    kb.button(text="➕ Новый профиль", callback_data=Menu(action="add_client"))
    pattern.append(2)
    kb.button(text="📢 Объявление пользователям", callback_data=BroadcastCB(action="pick"))
    pattern.append(1)
    kb.button(text="🔄 Статус", callback_data=Menu(action="refresh"))
    kb.button(text="⚙️ Настройки", callback_data=SetCB(sec="root"))
    pattern.append(2)
    kb.adjust(*pattern)
    return kb.as_markup()


def admin_clients(clients, online_ids=(), page: int = 0) -> InlineKeyboardMarkup:
    """Список профилей. Кружок — про ОНЛАЙН: зелёный, если подключён хотя бы
    один пир, иначе красный; ⏳ — профиль ещё не активировал доступ.

    Прежде кружок показывал состояние подписки, но оно и так видно в карточке
    (срок, блокировки), а вот «кто сейчас в сети» из списка узнать было негде.
    """
    online = set(online_ids or ())
    kb = InlineKeyboardBuilder()
    chunk, page, prev, nxt = page_slice(clients, page, static=1)
    for _i, c in chunk:
        mark = "⏳" if c.activation_status == ActivationStatus.PENDING else (
            "🟢" if c.id in online else "🔴")
        # админ видит все блокировки (включая тихие)
        blk = _blocks.blocked_marker_client(int(c.block_reason), for_admin=True)
        kb.button(text=f"{blk}{mark} {c.name}",
                  callback_data=ClientCB(action="open", client_id=c.id))
    nav = page_nav(kb, "clients", 0, page, prev, nxt, Menu(action="clients").pack())
    kb.button(text="⬅️ Назад", callback_data=Menu(action="main"))
    kb.adjust(*([1] * len(chunk)), *([nav] if nav else []), 1)
    return kb.as_markup()


def admin_client_actions(client, *, has_devices: bool = True,
                         is_admin_owner: bool = False,
                         routing_visible: bool = False,
                         routing_on: bool = False) -> InlineKeyboardMarkup:
    """has_devices=False скрывает «Выдать конфиг»/«Устройства» — клиенту
    нечего выдавать и нечего показывать в списке устройств.

    is_admin_owner=True — это клиент самого администратора: его НЕЛЬЗЯ
    блокировать, ограничивать (лимит трафика/устройств), ставить на паузу,
    продлевать (он бессрочный) или удалять. Оставляем только безопасные
    действия: выдать конфиг, устройства, добавить устройство, сменить имя."""
    kb = InlineKeyboardBuilder()
    pattern: list[int] = []
    # Вывод из приостановки — САМОЙ ВЕРХНЕЙ кнопкой и только пока клиент реально
    # на паузе (PAUSED-бит). Приоритет: это запасной выход из «отпускного» тупика
    # (клиент заперся в паузе, Telegram у него только через этот VPN).
    if not is_admin_owner and int(client.block_reason) & int(_blocks.ClientBlock.PAUSED):
        kb.button(text="▶️ Вывести из приостановки",
                  callback_data=ClientCB(action="resume_pause", client_id=client.id))
        pattern.append(1)
    # Перевыпуск инвайта — только пока инвайт не принят (pending): для
    # активированного клиента инвайт не нужен, кнопку не показываем.
    if client.activation_status == ActivationStatus.PENDING:
        kb.button(text="🔁 Перевыпустить инвайт",
                  callback_data=ClientCB(action="regen_invite", client_id=client.id))
        pattern.append(1)
    if has_devices:
        kb.button(text="🔗 Выдать конфиг", callback_data=ClientCB(action="gen_for", client_id=client.id))
        kb.button(text="📱 Устройства", callback_data=ClientCB(action="devices", client_id=client.id))
        pattern.append(2)
    kb.button(text="➕ Добавить устройство", callback_data=ClientCB(action="add_device", client_id=client.id))
    pattern.append(1)
    if is_admin_owner:
        # только безопасное: имя. Никаких блок/лимит/пауза/продлить/удалить.
        kb.button(text="✏️ Имя", callback_data=ClientCB(action="edit_name", client_id=client.id))
        pattern.append(1)
        # РФ-доступ — ВОЗМОЖНОСТЬ, а не ограничение, поэтому в урезанную ветку
        # входит: иначе у админа не было бы точки входа к своему переключателю.
        if routing_visible:
            kb.button(text=f"{_chk(routing_on)} Доступ к РФ-сервисам",
                      callback_data=RoutingCB(action="panel", ref=client.id))
            pattern.append(1)
        kb.button(text="⬅️ Назад", callback_data=Menu(action="clients"))
        pattern.append(1)
        kb.adjust(*pattern)
        return kb.as_markup()
    kb.button(text="⏱ Продлить", callback_data=ClientCB(action="extend", client_id=client.id))
    kb.button(text="✏️ Период", callback_data=ClientCB(action="edit_period", client_id=client.id))
    pattern.append(2)
    kb.button(text="✏️ Имя", callback_data=ClientCB(action="edit_name", client_id=client.id))
    kb.button(text="🔢 Лимит устройств", callback_data=ClientCB(action="edit_limit", client_id=client.id))
    pattern.append(2)
    kb.button(text="📊 Лимит потребления", callback_data=ClientCB(action="edit_traffic", client_id=client.id))
    pattern.append(1)
    # Клиентский переключатель РФ-доступа — админу тоже: он настраивает функцию
    # при разборе проблем, а гонять человека «включи у себя» ради проверки
    # значило бы делать поддержку невозможной. Над блокировкой намеренно:
    # это настройка, а не карательное действие.
    if routing_visible:
        kb.button(text=f"{_chk(routing_on)} Доступ к РФ-сервисам",
                  callback_data=RoutingCB(action="panel", ref=client.id))
        pattern.append(1)
    bt, bcb = _manual_block_button("cli", client.id, int(client.block_reason), for_admin=True)
    kb.button(text=bt, callback_data=bcb)
    pattern.append(1)
    kb.button(text="🗑 Удалить профиль", callback_data=ClientCB(action="delete", client_id=client.id))
    pattern.append(1)
    kb.button(text="⬅️ Назад", callback_data=Menu(action="clients"))
    pattern.append(1)
    kb.adjust(*pattern)
    return kb.as_markup()


def admin_client_back(client_id: int) -> InlineKeyboardMarkup:
    """Единственная кнопка — назад к карточке профиля.

    Для экранов-отбивок, после которых человек должен вернуться туда, откуда
    пришёл, и повторить действие (например, удаление не прошло).
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=ClientCB(action="open", client_id=client_id))
    return kb.as_markup()


def admin_client_device_list(devices, client_id: int, page: int = 0) -> InlineKeyboardMarkup:
    """Список устройств КОНКРЕТНОГО клиента (админ смотрит из его карточки).
    Тап открывает карточку устройства (DeviceCB open) — не генерит ссылку сразу.
    «Назад» — к карточке ЭТОГО клиента, не к общему списку клиентов."""
    kb = InlineKeyboardBuilder()
    chunk, page, prev, nxt = page_slice(devices, page, static=1)
    for _i, d in chunk:
        marker = _blocks.blocked_marker_device(int(d.block_reason), for_admin=True)
        kb.button(text=f"{marker}{_dev_emoji(d)} {d.name}", callback_data=DeviceCB(action="open", device_id=d.id))
    nav = page_nav(kb, "clidevs", client_id, page, prev, nxt,
                   ClientCB(action="devices", client_id=client_id).pack())
    kb.button(text="⬅️ Назад", callback_data=ClientCB(action="open", client_id=client_id))
    kb.adjust(*([1] * len(chunk)), *([nav] if nav else []), 1)
    return kb.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Устройства без клиента → привязка
# ─────────────────────────────────────────────────────────────────────────────

def unassigned_devices(devices, page: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    chunk, page, prev, nxt = page_slice(devices, page, static=1)
    for _i, d in chunk:
        kb.button(text=f"{d.name}{_btn_suffix(d)} — {d.address}",
                  callback_data=DeviceCB(action="open", device_id=d.id))
    nav = page_nav(kb, "unassigned", 0, page, prev, nxt, Menu(action="unassigned").pack())
    kb.button(text="⬅️ Назад", callback_data=Menu(action="main"))
    kb.adjust(*([1] * len(chunk)), *([nav] if nav else []), 1)
    return kb.as_markup()


def reassign_targets(device_id: int, clients, page: int = 0) -> InlineKeyboardMarkup:
    """Список клиентов, к которым можно привязать устройство без профиля."""
    kb = InlineKeyboardBuilder()
    chunk, page, prev, nxt = page_slice(clients, page, static=1)
    for _i, c in chunk:
        kb.button(text=c.name,
                  callback_data=ReassignCB(device_id=device_id, client_id=c.id, stage="go"))
    nav = page_nav(kb, "reassign", device_id, page, prev, nxt,
                   DeviceCB(action="reassign", device_id=device_id).pack())
    kb.button(text="⬅️ Назад", callback_data=Menu(action="unassigned"))
    kb.adjust(*([1] * len(chunk)), *([nav] if nav else []), 1)
    return kb.as_markup()


def reassign_addslot(device_id: int, client_id: int) -> InlineKeyboardMarkup:
    """Вопрос «добавить слот, раз лимит исчерпан?» при привязке устройства."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, добавить слот и привязать",
              callback_data=ReassignCB(device_id=device_id, client_id=client_id, stage="slot_yes"))
    kb.button(text="Отмена",
              callback_data=ReassignCB(device_id=device_id, client_id=client_id, stage="slot_no"))
    kb.adjust(1)
    return kb.as_markup()


def confirm_lower_limit() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2705 Да, применить", callback_data=ConfirmCB(action="lower_limit", yes=True))
    kb.button(text="\u2b05\ufe0f Отмена", callback_data=ConfirmCB(action="lower_limit", yes=False))
    kb.adjust(1)
    return kb.as_markup()


def traffic_profiles_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f В меню", callback_data=Menu(action="main"))
    return kb.as_markup()


def expiring_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f В меню", callback_data=Menu(action="main"))
    return kb.as_markup()


def online_devices_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f В меню", callback_data=Menu(action="main"))
    return kb.as_markup()


def traffic_devices_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f Назад", callback_data=Menu(action="traffic"))
    return kb.as_markup()


def rf_profiles_kb() -> InlineKeyboardMarkup:
    """Экран РФ-доступа по профилям — как разбивка потребления: одна «В меню»."""
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f В меню", callback_data=Menu(action="main"))
    return kb.as_markup()


def rf_devices_kb(back: str = "traffic_local") -> InlineKeyboardMarkup:
    """«Назад» — туда, откуда пришли: экран РФ-доступа или список трафика."""
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f Назад", callback_data=Menu(action=back))
    return kb.as_markup()
