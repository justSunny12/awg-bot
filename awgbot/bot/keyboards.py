"""
keyboards.py — инлайн-клавиатуры (aiogram). Callback-data берутся из callbacks.py.
"""

from __future__ import annotations

from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from awgbot.core import config
from awgbot.core import settings
from awgbot.bot import texts
from awgbot.core import blocks as _blocks
from awgbot.core.enums import SubStatus, ActivationStatus
from awgbot.bot.callbacks import HideCB

# ─────────────────────────────────────────────────────────────────────────────
# Reply-клавиатура (глобальные команды у поля ввода): «Меню» и «Отмена».
# Тексты кнонок — точные строки, по ним ловим в приоритетном роутере
# reply_commands. Эмодзи-префикс делает случайное совпадение с вводом
# (имя устройства и т.п.) практически невозможным.
# ─────────────────────────────────────────────────────────────────────────────

BTN_CANCEL = "\u2716\ufe0f Отмена"  # ✖️ Отмена


def reply_cancel() -> ReplyKeyboardMarkup:
    """Кнопка «Отмена» у поля ввода — на время текстового ввода."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True, is_persistent=True)


def reply_hide() -> ReplyKeyboardRemove:
    """Убрать reply-клавиатуру (когда открыто главное меню)."""
    return ReplyKeyboardRemove()

from awgbot.bot.callbacks import (AdminLinkGate, FaHintCB, AdminSelfCB, BlockCB, ClientCB, ConfirmCB, DelDeviceCB, DeviceCB,
                       FriendCB, GraceCB, GuideCB, HelpCB, Menu, PauseCB,
                       PeriodCB, ReassignCB, UpdateCB, SetCB)


# ─────────────────────────────────────────────────────────────────────────────
# Клиентские меню
# ─────────────────────────────────────────────────────────────────────────────

def client_main(has_devices: bool = True) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить устройство", callback_data=DeviceCB(action="add"))
    if has_devices:
        kb.button(text="📱 Мои устройства", callback_data=Menu(action="devices"))
    kb.button(text="⚙️ Управлять подпиской", callback_data=Menu(action="info"))
    if has_devices:
        kb.button(text="🔗 Получить ссылку", callback_data=Menu(action="gen_link"))
        kb.button(text="🔳 Получить QR-код", callback_data=Menu(action="gen_qr"))
        kb.button(text="📄 Получить файл", callback_data=Menu(action="gen_file"))
    kb.button(text="❓ Помощь с настройкой", callback_data=HelpCB(platform="root"))
    if has_devices:
        kb.adjust(1, 1, 1, 1, 2, 1)     # добавить/устройства/инфо / ссылка / [QR|файл] / помощь
    else:
        kb.adjust(1, 1, 1)          # добавить / инфо / помощь
    return kb.as_markup()


def _btn_suffix(dev) -> str:
    """Суффикс имени устройства для КНОПОК (HTML не рендерится): ФА → метка,
    app → звёздочка, управляемое → пусто. Единый источник для всех списков."""
    if getattr(dev, "is_admin", False):
        return " [Доступ к серверу]"
    if dev.is_app:
        return " *"
    return ""


def client_devices(devices) -> InlineKeyboardMarkup:
    """Список своих устройств. Без кнопки добавления — она уже есть в главном
    меню, дублировать здесь избыточно."""
    kb = InlineKeyboardBuilder()
    for d in devices:
        marker = _blocks.blocked_marker_device(int(d.block_reason), for_admin=False)
        kb.button(text=f"{marker}⚙️ {d.name}{_btn_suffix(d)}",
                  callback_data=DeviceCB(action="open", device_id=d.id))
    kb.button(text="⬅️ Назад", callback_data=Menu(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def admin_client_device_list(devices, client_id: int) -> InlineKeyboardMarkup:
    """Список устройств КОНКРЕТНОГО клиента (админ смотрит из его карточки).
    Тап открывает карточку устройства (DeviceCB open) — не генерит ссылку сразу.
    «Назад» — к карточке ЭТОГО клиента, не к общему списку клиентов."""
    kb = InlineKeyboardBuilder()
    for d in devices:
        marker = _blocks.blocked_marker_device(int(d.block_reason), for_admin=True)
        kb.button(text=f"{marker}⚙️ {d.name}", callback_data=DeviceCB(action="open", device_id=d.id))
    kb.button(text="⬅️ Назад", callback_data=ClientCB(action="open", client_id=client_id))
    kb.adjust(1)
    return kb.as_markup()


def device_actions(dev, *, is_admin: bool, back_target: str,
                    reassign_label: str = None) -> InlineKeyboardMarkup:
    """Единая карточка устройства — для ЛЮБОГО пути входа (свои устройства,
    устройства конкретного клиента, устройства без клиента). back_target —
    куда ведёт «Назад» (packed callback_data, вычисляется вызывающим кодом из
    принадлежности устройства — не тащим контекст «откуда пришли» через цепочку
    колбэков). reassign_label — текст кнопки привязки/перепривязки (только
    админ; None — кнопки нет, т.е. обычный клиент).

    bot-устройства: ссылка/QR/файл. app-устройства (без приватного ключа) не
    могут выдать ссылку — WireGuard держит приватный ключ только на самом
    устройстве. Поэтому предлагаем «прописать строку» (реставрация) и клиенту,
    и админу — иначе у клиента с app-устройством тупик.

    Удаление — ВСЕГДА через подтверждение (DelDeviceCB stage=ask), никогда не
    напрямую: последствия необратимы (ссылка глохнет, друг теряет доступ)."""
    kb = InlineKeyboardBuilder()

    # Full-access устройство (несёт ссылку полного доступа к серверу): особый
    # случай. Нельзя передавать/удалять/банить/менять лимит — это не пир бота, а
    # хранимая root-ссылка. Только ОДНО из двух: если ссылка уже сохранена —
    # выдать её (QR/ссылка/файл через гейт-предупреждение); если ещё нет —
    # реставрация (принять ссылку). Плюс «Назад».
    if getattr(dev, "is_admin", False):
        kb.button(text="✏️ Имя", callback_data=DeviceCB(action="edit_name", device_id=dev.id))
        kb.button(text="🔐 Получить ссылку доступа",
                  callback_data=DeviceCB(action="connect_menu", device_id=dev.id))
        kb.button(text="♻️ Изменить ссылку",
                  callback_data=DeviceCB(action="restore", device_id=dev.id))
        kb.button(text="↩️ Снять метку полного доступа",
                  callback_data=DeviceCB(action="clear_fa", device_id=dev.id))
        kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_target))
        kb.adjust(1, 1, 1, 1, 1)
        return kb.as_markup()

    is_bot_device = dev.is_managed
    rows = 0
    # 1) Данные для подключения (managed) / прописать строку (app)
    if is_bot_device:
        kb.button(text="🔌 Данные для подключения",
                  callback_data=DeviceCB(action="connect_menu", device_id=dev.id))
    else:
        kb.button(text="🔑 Прописать строку подключения",
                  callback_data=DeviceCB(action="restore", device_id=dev.id))
    rows += 1
    # 2) Имя
    kb.button(text="✏️ Имя", callback_data=DeviceCB(action="edit_name", device_id=dev.id))
    rows += 1
    # 3) Лимит потребления
    kb.button(text="📊 Лимит потребления", callback_data=DeviceCB(action="edit_traffic", device_id=dev.id))
    rows += 1
    # 4) Передать другу / перевыдать инвайт
    fstatus = dev.friend_status
    if is_bot_device and fstatus is None:
        kb.button(text="👤 Передать другу", callback_data=DeviceCB(action="transfer", device_id=dev.id))
        rows += 1
    elif fstatus == "pending":
        kb.button(text="🔁 Перевыдать инвайт", callback_data=DeviceCB(action="reinvite", device_id=dev.id))
        rows += 1
    # 5) Передать в другой профиль (только админ)
    if reassign_label:
        kb.button(text=reassign_label, callback_data=DeviceCB(action="reassign", device_id=dev.id))
        rows += 1
    # 6) Заблокировать
    bt, bcb = _manual_block_button("dev", dev.id, int(dev.block_reason), for_admin=is_admin)
    kb.button(text=bt, callback_data=bcb)
    rows += 1
    # 7) Удалить
    kb.button(text="🗑 Удалить", callback_data=DelDeviceCB(device_id=dev.id, stage="ask"))
    rows += 1
    # 8) Назад
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_target))
    kb.adjust(*([1] * rows), 1)
    return kb.as_markup()


def connect_method_choice(device_id: int, back_target: str) -> InlineKeyboardMarkup:
    """«Как планируешь подключить устройство?» — ссылка/QR/файл по одному в
    ряду. Для контекстов с DeviceCB (свои устройства, админ — любое устройство)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Получить ссылку", callback_data=DeviceCB(action="gen_link", device_id=device_id))
    kb.button(text="🔳 Получить QR-код", callback_data=DeviceCB(action="gen_qr", device_id=device_id))
    kb.button(text="📄 Получить файл", callback_data=DeviceCB(action="gen_file", device_id=device_id))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_target))
    kb.adjust(1, 2)                 # ссылка / [QR|файл] (ряд «Назад» — отдельно)
    return kb.as_markup()





def fa_pick_devices(devices, ct_names: dict) -> InlineKeyboardMarkup:
    """Выбор устройства для назначения полного доступа. Показываем имя из
    clientsTable (если есть) + IP в [скобках] — админ опознаёт admin-пир.
    Выбор ведёт в ПОДТВЕРЖДЕНИЕ назначения (с показом имени+IP)."""
    kb = InlineKeyboardBuilder()
    for d in devices:
        ct = ct_names.get(d.public_key) or d.name
        kb.button(text=f"{ct} [{d.address}]",
                  callback_data=DeviceCB(action="fa_assign", device_id=d.id))
    kb.button(text="⬅️ Назад", callback_data=Menu(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def fa_assign_confirm(device_id: int) -> InlineKeyboardMarkup:
    """Подтверждение назначения ФА выбранному устройству → далее ввод ссылки."""
    kb = InlineKeyboardBuilder()
    kb.button(text="➡️ Продолжить",
              callback_data=DeviceCB(action="fa_link", device_id=device_id))
    kb.button(text="⬅️ Назад", callback_data=FaHintCB(action="choose"))
    kb.adjust(1, 1)
    return kb.as_markup()


def admin_fa_hint() -> InlineKeyboardMarkup:
    """Кнопки под стартовой подсветкой: выбрать устройство / игнорировать."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Выбрать устройство", callback_data=FaHintCB(action="choose"))
    kb.button(text="🚫 Игнорировать", callback_data=FaHintCB(action="ignore"))
    kb.adjust(1, 1)
    return kb.as_markup()




def confirm_clear_fa(device_id: int) -> InlineKeyboardMarkup:
    """Подтверждение снятия метки полного доступа (ссылка утрачивается)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Да, снять метку",
              callback_data=AdminLinkGate(device_id=device_id, method="clear", confirm=True))
    kb.button(text="Отмена", callback_data=DeviceCB(action="open", device_id=device_id))
    kb.adjust(1, 1)
    return kb.as_markup()


def confirm_change_fa_link(device_id: int) -> InlineKeyboardMarkup:
    """Подтверждение замены ссылки полного доступа (прежняя утрачивается)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="♻️ Да, заменить",
              callback_data=AdminLinkGate(device_id=device_id, method="change", confirm=True))
    kb.button(text="Отмена", callback_data=DeviceCB(action="open", device_id=device_id))
    kb.adjust(1, 1)
    return kb.as_markup()


def admin_link_gate(device_id: int, method: str) -> InlineKeyboardMarkup:
    """Диалог-предупреждение перед выдачей ссылки полного доступа: «Я понимаю,
    отдай» (confirm=True) / «Убедил, отмена» (возврат к карточке устройства)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="Я понимаю, отдай",
              callback_data=AdminLinkGate(device_id=device_id, method=method, confirm=True))
    kb.button(text="Убедил, отмена",
              callback_data=DeviceCB(action="open", device_id=device_id))
    kb.adjust(1, 1)
    return kb.as_markup()


def connect_method_choice_friend(device_id: int) -> InlineKeyboardMarkup:
    """То же самое, но для друга — колбэки FriendCB (свой namespace), назад —
    к карточке устройства друга."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Получить ссылку", callback_data=FriendCB(action="gen_link", device_id=device_id))
    kb.button(text="🔳 Получить QR-код", callback_data=FriendCB(action="gen_qr", device_id=device_id))
    kb.button(text="📄 Получить файл", callback_data=FriendCB(action="gen_file", device_id=device_id))
    kb.button(text="⬅️ Назад", callback_data=FriendCB(action="open", device_id=device_id))
    kb.adjust(1, 2, 1)             # ссылка / [QR|файл] / Назад
    return kb.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Выбор устройства для генерации (клиент/админ жмёт «получить ссылку/файл»)
# ─────────────────────────────────────────────────────────────────────────────

def pick_device(devices, action: str, back_cb: str = None) -> InlineKeyboardMarkup:
    """action: gen_link | gen_file | gen_qr | connect_menu — выбор устройства.
    Показываем и app-устройства (с суффиксом): клик по ним ведёт не в ошибку,
    а в диалог «пришли ссылку или удали» (обрабатывается отдельно).
    back_cb — packed callback для «Назад» (по умолчанию главное меню; админ из
    карточки клиента передаёт возврат в карточку)."""
    kb = InlineKeyboardBuilder()
    for d in devices:
        kb.button(text=f"{d.name}{_btn_suffix(d)}",
                  callback_data=DeviceCB(action=action, device_id=d.id))
    kb.row(InlineKeyboardButton(
        text="⬅️ Назад", callback_data=back_cb or Menu(action="main").pack()))
    kb.adjust(1)
    return kb.as_markup()


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


def pick_client_for_add_device(clients) -> InlineKeyboardMarkup:
    """Список клиентов для «Добавить устройство → другому клиенту»."""
    kb = InlineKeyboardBuilder()
    for c in clients:
        kb.button(text=c.name, callback_data=ClientCB(action="add_device", client_id=c.id))
    kb.button(text="⬅️ Назад", callback_data=Menu(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def admin_main(unassigned_count: int, self_has_devices: bool = False) -> InlineKeyboardMarkup:
    """Главное меню админа. Личный блок (он тоже пользователь VPN) сверху,
    затем управление клиентской базой. «Добавить устройство» ведёт в диалог
    выбора (себе/другому клиенту) — там же гейт по личному лимиту, а не тут:
    другому клиенту добавлять можно и при исчерпанном личном лимите."""
    kb = InlineKeyboardBuilder()
    pattern: list[int] = []
    kb.button(text="➕ Добавить устройство", callback_data=Menu(action="add_device_choice"))
    pattern.append(1)
    if self_has_devices:
        kb.button(text="📱 Мои устройства", callback_data=AdminSelfCB(action="devices"))
        pattern.append(1)
        kb.button(text="🔗 Получить ссылку", callback_data=AdminSelfCB(action="gen_link"))
        pattern.append(1)
        kb.button(text="🔳 Получить QR-код", callback_data=AdminSelfCB(action="gen_qr"))
        kb.button(text="📄 Получить файл", callback_data=AdminSelfCB(action="gen_file"))
        pattern.append(2)
    if unassigned_count > 0:
        kb.button(text=f"📦 Устройства без профиля ({unassigned_count})",
                  callback_data=Menu(action="unassigned"))
        pattern.append(1)
    kb.button(text="👥 Профили", callback_data=Menu(action="clients"))
    kb.button(text="➕ Новый профиль", callback_data=Menu(action="add_client"))
    pattern.append(2)
    kb.button(text="🔄 Статус", callback_data=Menu(action="refresh"))
    kb.button(text="⚙️ Настройки", callback_data=SetCB(sec="root"))
    pattern.append(2)
    kb.adjust(*pattern)
    return kb.as_markup()


def admin_clients(clients) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in clients:
        mark = "⏳" if c.activation_status == ActivationStatus.PENDING else (
            "🟢" if c.status == SubStatus.ACTIVE else "🔴")
        # админ видит все блокировки (включая тихие)
        blk = _blocks.blocked_marker_client(int(c.block_reason), for_admin=True)
        kb.button(text=f"{blk}{mark} {c.name}",
                  callback_data=ClientCB(action="open", client_id=c.id))
    kb.button(text="⬅️ Назад", callback_data=Menu(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def admin_client_actions(client, *, has_devices: bool = True,
                         is_admin_owner: bool = False) -> InlineKeyboardMarkup:
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
    bt, bcb = _manual_block_button("cli", client.id, int(client.block_reason), for_admin=True)
    kb.button(text=bt, callback_data=bcb)
    pattern.append(1)
    kb.button(text="🗑 Удалить профиль", callback_data=ClientCB(action="delete", client_id=client.id))
    pattern.append(1)
    kb.button(text="⬅️ Назад", callback_data=Menu(action="clients"))
    pattern.append(1)
    kb.adjust(*pattern)
    return kb.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Выбор периода (создание/продление)
# ─────────────────────────────────────────────────────────────────────────────

def period_choices(ctx: str, ref: int = 0, min_days: int = 0) -> InlineKeyboardMarkup:
    """ctx: create | extend. ref: id клиента при продлении.
    min_days: скрыть периоды короче/равные (после вычета отсрочки остался бы ноль
    или минус). «never» не отсекается никогда — вычитать из безлимита нечего.
    Минимальные длительности kind'ов берём консервативно (month=28, year=365),
    чтобы гарантированно не показать период, который может оказаться коротким."""
    _MIN_DAYS = {"day": 1, "week": 7, "month": 28, "year": 365}
    kb = InlineKeyboardBuilder()
    n = 0
    for kind in config.PERIOD_CHOICES:
        if kind != "never" and _MIN_DAYS.get(kind, 0) <= min_days:
            continue
        kb.button(text=config.PERIOD_LABELS[kind],
                  callback_data=PeriodCB(kind=kind, ctx=ctx, ref=ref))
        n += 1
    # Кнопка выхода: при продлении — назад к карточке клиента; при создании —
    # отмена в главное меню. Без неё диалог выбора срока — тупик (был баг).
    if ctx == "extend" and ref:
        kb.button(text="⬅️ Отмена", callback_data=ClientCB(action="open", client_id=ref))
    else:
        kb.button(text="⬅️ Отмена", callback_data=Menu(action="main"))
    # периоды по 2 в ряд, кнопка отмены — отдельной строкой снизу
    rows = [2] * (n // 2) + ([1] if n % 2 else []) + [1]
    kb.adjust(*rows)
    return kb.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Да/Нет
# ─────────────────────────────────────────────────────────────────────────────

def yes_no(action: str, ref: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Да", callback_data=ConfirmCB(action=action, ref=ref, yes=True))
    kb.button(text="Нет", callback_data=ConfirmCB(action=action, ref=ref, yes=False))
    kb.adjust(2)
    return kb.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Устройства без клиента → привязка
# ─────────────────────────────────────────────────────────────────────────────

def unassigned_devices(devices) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for d in devices:
        kb.button(text=f"{d.name}{_btn_suffix(d)} — {d.address}",
                  callback_data=DeviceCB(action="open", device_id=d.id))
    kb.button(text="⬅️ Назад", callback_data=Menu(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def reassign_targets(device_id: int, clients) -> InlineKeyboardMarkup:
    """Список клиентов, к которым можно привязать app-устройство."""
    kb = InlineKeyboardBuilder()
    for c in clients:
        kb.button(text=c.name,
                  callback_data=ReassignCB(device_id=device_id, client_id=c.id, stage="go"))
    kb.button(text="⬅️ Назад", callback_data=Menu(action="unassigned"))
    kb.adjust(1)
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


# ─────────────────────────────────────────────────────────────────────────────
# Меню помощи с настройкой (постактивационное и постоянное)
# ─────────────────────────────────────────────────────────────────────────────

def confirm_lower_limit() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2705 Да, применить", callback_data=ConfirmCB(action="lower_limit", yes=True))
    kb.button(text="\u2b05\ufe0f Отмена", callback_data=ConfirmCB(action="lower_limit", yes=False))
    kb.adjust(1)
    return kb.as_markup()


def confirm_transfer(device_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="\U0001F464 Да, передать другу",
              callback_data=DeviceCB(action="transfer_yes", device_id=device_id))
    kb.button(text="\u2b05\ufe0f Отмена",
              callback_data=DeviceCB(action="open", device_id=device_id))
    kb.adjust(1)
    return kb.as_markup()


def add_for_whom() -> InlineKeyboardMarkup:
    """Выбор перед именем: устройство себе или для друга (с инвайтом)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Себе", callback_data=DeviceCB(action="add_self"))
    kb.button(text="👤 Другу", callback_data=DeviceCB(action="add_friend"))
    kb.button(text="⬅️ Назад", callback_data=Menu(action="main"))
    kb.adjust(2, 1)
    return kb.as_markup()


def help_menu(is_initial: bool = False) -> InlineKeyboardMarkup:
    """is_initial=True — самый первый гайд сразу после активации: без «В меню»
    (идти пока некуда), «Всё знаю» — единственный способ пропустить, внизу
    (сначала предлагаем платформы). Обычный вызов (из «Помощь с настройкой» в
    меню, доступно в любой момент) — «Всё знаю» не нужен вовсе: «В меню» уже
    покрывает ту же роль («пропустить, я и так знаю» = просто выйти в меню)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🍎 У меня iPhone / iPad", callback_data=HelpCB(platform="apple"))
    kb.button(text="🤖 У меня Android", callback_data=HelpCB(platform="android"))
    kb.button(text="🪟 У меня Windows", callback_data=HelpCB(platform="windows"))
    kb.button(text="🍏 У меня Mac", callback_data=HelpCB(platform="mac"))
    if is_initial:
        kb.button(text="✅ Всё знаю и умею", callback_data=HelpCB(platform="skip"))
    else:
        kb.button(text="⬅️ В меню", callback_data=Menu(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def to_menu() -> InlineKeyboardMarkup:
    """Одна кнопка «В меню» — завершитель под контентом (admin/client)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f В меню", callback_data=Menu(action="main"))
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


def update_admin_available() -> InlineKeyboardMarkup:
    """Админ-проверка «Обновление бота» с доступной версией: Обновить / Назад."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬆️ Обновить", callback_data=UpdateCB(action="install"))
    kb.button(text="\u2b05\ufe0f Назад", callback_data=Menu(action="main"))
    kb.adjust(2)
    return kb.as_markup()


def update_done_menu() -> InlineKeyboardMarkup:
    """«В меню» на итоговом сообщении self-update. Свой колбэк (upd:menu), а не
    Menu(main): стандартный обработчик РЕДАКТИРУЕТ сообщение в панель, а итог
    должен остаться в истории — кнопка лишь снимается, меню приходит новым
    сообщением."""
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f В меню", callback_data=UpdateCB(action="menu"))
    return kb.as_markup()


def friend_finisher() -> InlineKeyboardMarkup:
    """Завершитель под контентом для друга — возврат к его панели."""
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f К устройству", callback_data=FriendCB(action="refresh"))
    return kb.as_markup()


def friend_main(device_id: int = 0, *, multi: bool = False) -> InlineKeyboardMarkup:
    """Меню друга над КОНКРЕТНЫМ устройством: как подключить/помощь/обновить.
    device_id прокидываем в действия (у друга может быть >1 устройства).
    multi=True добавляет «к списку устройств»."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔌 Данные для подключения", callback_data=FriendCB(action="connect_menu", device_id=device_id))
    kb.button(text="❓ Помощь с подключением", callback_data=FriendCB(action="help", device_id=device_id))
    kb.button(text="🔄 Обновить", callback_data=FriendCB(action="refresh", device_id=device_id))
    if multi:
        kb.button(text="⬅️ К моим устройствам", callback_data=FriendCB(action="list"))
    kb.adjust(1)
    return kb.as_markup()


def friend_device_list(devices) -> InlineKeyboardMarkup:
    """Список устройств друга (когда их несколько) — выбор, какое открыть."""
    kb = InlineKeyboardBuilder()
    for d in devices:
        kb.button(text=texts.device_label(d),
                  callback_data=FriendCB(action="open", device_id=d.id))
    kb.adjust(1)
    return kb.as_markup()


def friend_help_back() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f Назад", callback_data=FriendCB(action="list"))
    kb.adjust(1)
    return kb.as_markup()


def friend_help_menu() -> InlineKeyboardMarkup:
    """Помощь для друга — те же платформы, но возврат в friend-панель."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🍎 У меня iPhone / iPad", callback_data=HelpCB(platform="apple"))
    kb.button(text="🤖 У меня Android", callback_data=HelpCB(platform="android"))
    kb.button(text="🪟 У меня Windows", callback_data=HelpCB(platform="windows"))
    kb.button(text="🍏 У меня Mac", callback_data=HelpCB(platform="mac"))
    kb.button(text="⬅️ Назад", callback_data=FriendCB(action="refresh"))
    kb.adjust(1)
    return kb.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Удаление устройства: обычное и усиленное (единственное)
# ─────────────────────────────────────────────────────────────────────────────

def confirm_delete_device(device_id: int, only: bool) -> InlineKeyboardMarkup:
    """«Отмена» ведёт к карточке ЭТОГО устройства (DeviceCB open) — карточка
    контекстно-корректна для любой роли и точки входа. Прежний Menu(devices)
    у админа уводил в ЕГО СОБСТВЕННЫЙ список устройств, даже когда он удалял
    устройство клиента или бесхозное."""
    kb = InlineKeyboardBuilder()
    if only:
        # усиленное: явная кнопка с признанием риска
        kb.button(text="⚠️ Да, понимаю риск — удалить",
                  callback_data=DelDeviceCB(device_id=device_id, stage="confirm"))
    else:
        kb.button(text="🗑 Да, удалить",
                  callback_data=DelDeviceCB(device_id=device_id, stage="confirm"))
    kb.button(text="Отмена", callback_data=DeviceCB(action="open", device_id=device_id))
    kb.adjust(1)
    return kb.as_markup()


def pick_device_to_delete(devices) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for d in devices:
        kb.button(text=f"🗑 {d.name}", callback_data=DelDeviceCB(device_id=d.id, stage="ask"))
    kb.button(text="⬅️ Назад", callback_data=Menu(action="devices"))
    kb.adjust(1)
    return kb.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Клиентское уведомление о добавленном админом устройстве
# ─────────────────────────────────────────────────────────────────────────────

def added_by_admin(device_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Получить ссылку", callback_data=DeviceCB(action="gen_link", device_id=device_id))
    kb.button(text="📄 Получить файл", callback_data=DeviceCB(action="gen_file", device_id=device_id))
    kb.button(text="❓ Помощь с настройкой", callback_data=HelpCB(platform="root"))
    kb.adjust(2, 1)
    return kb.as_markup()


def app_device_dialog(device_id: int) -> InlineKeyboardMarkup:
    """Диалог при клике на app-устройство в списке «получить ссылку»:
    прислать строку / удалить / назад."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔑 Отправить ссылку", callback_data=DeviceCB(action="restore", device_id=device_id))
    kb.button(text="🗑 Удалить устройство", callback_data=DelDeviceCB(device_id=device_id, stage="ask"))
    kb.button(text="⬅️ Назад", callback_data=Menu(action="main"))
    kb.adjust(1)
    return kb.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Навигация по визарду-гайду
# ─────────────────────────────────────────────────────────────────────────────

def guide_nav(guide: str, step: int, last: int, *, next_guide: str = None,
              apple_connect_end: bool = False) -> InlineKeyboardMarkup:
    """Кнопки под шагом гайда: Назад / Далее (или переход к следующему гайду),
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
        # последний шаг установочного гайда → переход к подключению
        kb.button(text="📶 К подключению", callback_data=GuideCB(guide=next_guide, step=0))
        row += 1
    # спец-кнопка: в конце подключения на Apple предлагаем гайд про шторку
    if apple_connect_end:
        kb.button(text="🎛 Переключатель в шторку", callback_data=GuideCB(guide="toggle", step=0))
    kb.button(text="🏠 В меню", callback_data=Menu(action="main"))
    # раскладка: навигация в ряд, спецкнопка и «в меню» — отдельными строками
    if apple_connect_end:
        kb.adjust(row if row else 1, 1, 1)
    else:
        kb.adjust(row if row else 1, 1)
    return kb.as_markup()


def guide_connect_method(device_id: int, guide: str) -> InlineKeyboardMarkup:
    """Шаг 1 «Настраиваем подключение»: выбор способа (ссылка/QR/файл) для
    выбранного устройства. По выбору бот выдаёт артефакт и ведёт на шаг 2
    «Подключаемся». «Назад» — к выбору устройства (шаг 0)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Получить ссылку",
              callback_data=GuideCB(guide=guide, step=1, dev=device_id, kind="link"))
    kb.button(text="🔳 Получить QR-код",
              callback_data=GuideCB(guide=guide, step=1, dev=device_id, kind="qr"))
    kb.button(text="📄 Получить файл",
              callback_data=GuideCB(guide=guide, step=1, dev=device_id, kind="file"))
    kb.button(text="⬅️ Назад", callback_data=GuideCB(guide=guide, step=0))
    kb.button(text="🏠 В меню", callback_data=Menu(action="main"))
    kb.adjust(1, 2, 1, 1)          # ссылка / [QR|файл] / Назад / В меню
    return kb.as_markup()


def guide_connect_done(guide: str, device_id: int, *, apple_end: bool) -> InlineKeyboardMarkup:
    """Шаг 2 «Подключаемся» (после выдачи артефакта): «Назад» — к выбору способа
    для того же устройства; для Apple — гайд про переключатель в шторку; выход в
    меню."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=GuideCB(guide=guide, step=1, dev=device_id))
    if apple_end:
        kb.button(text="🎛 Переключатель в шторку", callback_data=GuideCB(guide="toggle", step=0))
    kb.button(text="🏠 В меню", callback_data=Menu(action="main"))
    kb.adjust(1, 1, 1)
    return kb.as_markup()


def guide_connect_devices(devices, slots, last: int, guide: str = "connect") -> InlineKeyboardMarkup:
    """Шаг 0 подключения: «Добавить устройство» + существующие устройства.
    Кнопки устройств/добавления сами ведут дальше (выдают ссылку+файл и переводят
    на шаг настройки) — отдельной «Далее» нет. guide сохраняет вариант."""
    used, limit = slots
    kb = InlineKeyboardBuilder()
    if limit == 0 or used < limit:      # 0 = безлимит
        kb.button(text="➕ Добавить устройство", callback_data=GuideCB(guide=guide, step=-1))
    for d in devices:
        # получить ссылку+файл этого устройства и перейти к шагу настройки
        kb.button(text=f"🔗 {d.name}",
                  callback_data=DeviceCB(action="gen_guide", device_id=d.id))
    kb.button(text="🏠 В меню", callback_data=Menu(action="main"))
    kb.adjust(1)
    return kb.as_markup()


__all__ = [
    "client_main", "client_devices", "device_actions", "pick_device",
    "admin_main", "admin_clients", "admin_client_actions", "period_choices",
    "yes_no", "unassigned_devices", "reassign_targets",
]


def append_hide_row(kb: InlineKeyboardBuilder) -> InlineKeyboardMarkup:
    """Добавляет «Скрыть» ПОСЛЕДНЕЙ строкой к уже собранной клавиатуре и
    возвращает готовую разметку. Используется везде, где у проактивного
    уведомления есть свои кнопки действия (сейчас — только grace_offer)."""
    kb.row(InlineKeyboardButton(text="Скрыть", callback_data=HideCB().pack()))
    return kb.as_markup()


def hide_only() -> InlineKeyboardMarkup:
    """Клавиатура из одной кнопки «Скрыть» — дефолт для проактивных уведомлений
    без собственных кнопок действия (notifier подставляет её автоматически)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="Скрыть", callback_data=HideCB())
    return kb.as_markup()


def grace_offer(client_id: int, days: int) -> InlineKeyboardMarkup:
    """Кнопки в уведомлении об истечении (только клиент, годовой период, 1 раз):
    активировать отсрочку или скрыть уведомление (последней строкой — как и
    везде на проактивных уведомлениях)."""
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Продли чуток? 🙏 (+{days} дн.)",
              callback_data=GraceCB(action="take", ref=client_id))
    kb.adjust(1)
    return append_hide_row(kb)


# ── Ручные блокировки ────────────────────────────────────────────────────────


def _manual_block_button(target: str, ref: int, mask: int, *, for_admin: bool):
    """Кнопка «Заблокировать»/«Разблокировать» для карточки.
    Админ управляет всеми ручными битами → смотрит на всю ручную маску.
    Клиент управляет ТОЛЬКО своим USER-битом → кнопка отражает лишь его: если
    сам заблокировал → «Разблокировать», иначе «Заблокировать». Админские биты
    на его устройстве клиент кнопкой не снимет (и кнопка это не обещает)."""
    if for_admin:
        manual = _blocks.DEVICE_MANUAL if target == "dev" else _blocks.CLIENT_MANUAL
        has_manual = int(mask) & int(manual)
    else:
        user_bit = (_blocks.DeviceBlock.USER if target == "dev"
                    else _blocks.ClientBlock.USER)
        has_manual = int(mask) & int(user_bit)
    if has_manual:
        return ("✅ Разблокировать", BlockCB(target=target, action="menu_unblock", ref=ref))
    return ("🛑 Заблокировать", BlockCB(target=target, action="menu_block", ref=ref))


def block_pause_choice(client_id: int) -> InlineKeyboardMarkup:
    """Блок клиента: приостановить ли подписку на время блокировки."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⏸ Да, приостановить подписку",
              callback_data=BlockCB(target="cli", action="pause_yes", ref=client_id))
    kb.button(text="▶️ Нет, подписка тикает",
              callback_data=BlockCB(target="cli", action="pause_no", ref=client_id))
    kb.button(text="⬅️ Отмена", callback_data=BlockCB(target="cli", action="cancel", ref=client_id))
    kb.adjust(1)
    return kb.as_markup()


def block_notify_choice(target: str, ref: int, pause_days: int = -1) -> InlineKeyboardMarkup:
    """Админ ставит блок: уведомить пользователя или тихо. pause_days — режим
    приостановки (в отдельном поле days): -1 без паузы, 0 бессрочно, N срочная."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔔 С уведомлением",
              callback_data=BlockCB(target=target, action="block", ref=ref, kind="notified", days=pause_days))
    kb.button(text="🔕 Тихо (не уведомлять)",
              callback_data=BlockCB(target=target, action="block", ref=ref, kind="silent", days=pause_days))
    kb.button(text="⬅️ Отмена", callback_data=BlockCB(target=target, action="cancel", ref=ref))
    kb.adjust(1)
    return kb.as_markup()


def block_unblock_reasons(target: str, ref: int, mask: int) -> InlineKeyboardMarkup:
    """Админ снимает блок: перечислить активные РУЧНЫЕ причины + «Снять всё»
    (если больше одной). Если причина ровно одна — этот экран не показываем
    вовсе (см. admin_unblock_menu), снимаем сразу."""
    kb = InlineKeyboardBuilder()
    if target == "dev":
        items = [("silent", _blocks.DeviceBlock.ADMIN_SILENT, "Тихий админ-блок"),
                 ("notified", _blocks.DeviceBlock.ADMIN_NOTIFIED, "Админ-блок"),
                 ("user", _blocks.DeviceBlock.USER, "Блок владельца")]
    else:
        items = [("silent", _blocks.ClientBlock.ADMIN_SILENT, "Тихий админ-блок"),
                 ("notified", _blocks.ClientBlock.ADMIN_NOTIFIED, "Админ-блок"),
                 ("user", _blocks.ClientBlock.USER, "Блок владельца")]
    active = [(kind, lbl) for kind, bit, lbl in items if int(mask) & int(bit)]
    for kind, lbl in active:
        kb.button(text=lbl,
                  callback_data=BlockCB(target=target, action="unblock", ref=ref, kind=kind))
    if len(active) > 1:
        kb.button(text="Снять всё",
                  callback_data=BlockCB(target=target, action="unblock", ref=ref, kind="all"))
    kb.button(text="⬅️ Отмена", callback_data=BlockCB(target=target, action="cancel", ref=ref))
    kb.adjust(1)
    return kb.as_markup()


# ── Приостановка подписки (клиент) ───────────────────────────────────────────

def client_info_actions(client, *, paused: bool, can_pause: bool) -> InlineKeyboardMarkup:
    """Кнопки под «Управлять подпиской»: в меню + приостановка/возобновление.
    Кнопка паузы только для годовой подписки (can_pause), возобновление — если
    сейчас на паузе (ведёт на подтверждение — сколько дней спишется)."""
    kb = InlineKeyboardBuilder()
    if paused:
        kb.button(text="▶️ Возобновить подписку",
                  callback_data=PauseCB(action="resume_ask", ref=client.id))
    elif can_pause:
        kb.button(text="⏸ Приостановить (в отпуск)",
                  callback_data=PauseCB(action="ask", ref=client.id))
    kb.button(text="⬅️ В меню", callback_data=Menu(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def pause_day_choice(client_id: int, available: int) -> InlineKeyboardMarkup:
    """Выбор длительности приостановки. Пресеты 7/14 показываем только если они
    ≤ доступного (недоступные не выводим). Кнопку «весь доступный» даём просто
    числом «{available} дн.» — и только если это число не совпало с уже
    показанным пресетом. «Другое» — ввод своего числа."""
    kb = InlineKeyboardBuilder()
    shown = [p for p in (7, 14) if p <= available]
    for preset in shown:
        kb.button(text=f"{preset} дн.",
                  callback_data=PauseCB(action="pick", ref=client_id, days=preset))
    if available not in shown:
        kb.button(text=f"{available} дн.",
                  callback_data=PauseCB(action="pick", ref=client_id, days=available))
    # «Другое» имеет смысл только если есть что вводить помимо готовых кнопок:
    # при available < 2 остаётся лишь «1 дн.» (0 не принимаем) — кнопку убираем.
    other = available >= 2
    if other:
        kb.button(text="✏️ Другое", callback_data=PauseCB(action="other", ref=client_id))
    kb.button(text="⬅️ Отмена", callback_data=PauseCB(action="cancel", ref=client_id))
    n = len(shown) + (0 if available in shown else 1) + (1 if other else 0)
    rows = [2] * (n // 2) + ([1] if n % 2 else []) + [1]
    kb.adjust(*rows)
    return kb.as_markup()


def pause_confirm(client_id: int, days: int) -> InlineKeyboardMarkup:
    """Подтверждение входа в приостановку на выбранное число дней (после варнинга)."""
    kb = InlineKeyboardBuilder()
    kb.button(text=f"⏸ Приостановить на {days} дн.",
              callback_data=PauseCB(action="confirm", ref=client_id, days=days))
    kb.button(text="⬅️ Отмена", callback_data=PauseCB(action="cancel", ref=client_id))
    kb.adjust(1)
    return kb.as_markup()


def pause_resume_confirm(client_id: int) -> InlineKeyboardMarkup:
    """Подтверждение выхода из паузы раньше срока — с явным указанием (в
    тексте инфобокса), что спишутся фактические дни, а не весь резерв."""
    kb = InlineKeyboardBuilder()
    kb.button(text="▶️ Да, возобновить сейчас", callback_data=PauseCB(action="resume", ref=client_id))
    kb.button(text="⬅️ Отмена", callback_data=PauseCB(action="cancel", ref=client_id))
    kb.adjust(1)
    return kb.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Экран «⚙️ Настройки» (админ). Значения читаются из settings в момент рендера —
# после правки экран перерисовывается и показывает актуальное.
# ─────────────────────────────────────────────────────────────────────────────
def _chk(on: bool) -> str:
    return "✅" if on else "⬜"


def settings_root() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔔 Уведомления", callback_data=SetCB(sec="notify"))
    kb.button(text="💳 Параметры подписок", callback_data=SetCB(sec="subs"))
    kb.button(text="📊 Мониторинг", callback_data=SetCB(sec="mon"))
    kb.button(text="💾 Резервное копирование", callback_data=SetCB(sec="backup"))
    kb.button(text="🔄 Сервис", callback_data=SetCB(sec="svc"))
    kb.button(text="⬆️ Обновления бота", callback_data=SetCB(sec="upd"))
    kb.button(text="\u2b05\ufe0f В меню", callback_data=Menu(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def _back(sec_to: str = "root") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="\u2b05\ufe0f Назад", callback_data=SetCB(sec=sec_to).pack())


def settings_notify() -> InlineKeyboardMarkup:
    s = settings
    kb = InlineKeyboardBuilder()
    qh = s.get_bool("quiet_hours.quiet_hours_enabled", True)
    kb.button(text=f"{_chk(qh)} Тихие часы",
              callback_data=SetCB(sec="notify", act="toggle", key="quiet_hours.quiet_hours_enabled"))
    if qh:
        kb.button(text=f"Начало: {s.get_int('quiet_hours.quiet_hours_start', 20)}:00",
                  callback_data=SetCB(sec="notify", act="edit", key="quiet_hours.quiet_hours_start"))
        kb.button(text=f"Конец: {s.get_int('quiet_hours.quiet_hours_end', 7)}:00",
                  callback_data=SetCB(sec="notify", act="edit", key="quiet_hours.quiet_hours_end"))
    ra = s.get_bool("resource_alerts.enabled", True)
    kb.button(text=f"{_chk(ra)} Алерты хоста (CPU/RAM/диск)",
              callback_data=SetCB(sec="notify", act="toggle", key="resource_alerts.enabled"))
    if ra:
        kb.button(text=f"CPU: {s.get_int('resource_alerts.thresholds_percent.cpu', 80)}%",
                  callback_data=SetCB(sec="notify", act="edit", key="resource_alerts.thresholds_percent.cpu"))
        kb.button(text=f"RAM: {s.get_int('resource_alerts.thresholds_percent.ram', 80)}%",
                  callback_data=SetCB(sec="notify", act="edit", key="resource_alerts.thresholds_percent.ram"))
        kb.button(text=f"Диск: {s.get_int('resource_alerts.thresholds_percent.disk', 80)}%",
                  callback_data=SetCB(sec="notify", act="edit", key="resource_alerts.thresholds_percent.disk"))
    ce = "notifications.client_events"
    for k, label in (("activation", "Активация клиента"), ("grace", "Самопродление"),
                     ("over_limit", "Превышение лимита"), ("bonus", "Выдача бонуса")):
        on = s.get_bool(f"{ce}.{k}", True)
        kb.button(text=f"{_chk(on)} {label}",
                  callback_data=SetCB(sec="notify", act="toggle", key=f"{ce}.{k}"))
    # раскладка: тихие часы (1) [+ начало/конец (2)] + алерты (1) [+ 3 порога] +
    # 4 события клиентов — по одной кнопке в ряд для читаемости
    rows = [1] + ([2] if qh else []) + [1] + ([1, 1, 1] if ra else []) + [1, 1, 1, 1]
    kb.adjust(*rows)
    kb.row(_back())
    return kb.as_markup()


def settings_subs() -> InlineKeyboardMarkup:
    s = settings
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Бонус-квота: {s.get_int('limits.traffic_bonus_gb', 100)} ГБ",
              callback_data=SetCB(sec="subs", act="edit", key="limits.traffic_bonus_gb"))
    kb.button(text=f"Макс. дней паузы: {s.get_int('pause.pause_max_total_days', 28)}",
              callback_data=SetCB(sec="subs", act="edit", key="pause.pause_max_total_days"))
    kb.button(text=f"Grace-дней: {s.get_int('grace.grace_days', 14)}",
              callback_data=SetCB(sec="subs", act="edit", key="grace.grace_days"))
    kb.adjust(1)
    kb.row(_back())
    return kb.as_markup()


def settings_mon() -> InlineKeyboardMarkup:
    s = settings
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Частота опроса: {s.get_int('app.scheduler.monitor_minutes', 3)} мин",
              callback_data=SetCB(sec="mon", act="edit", key="app.scheduler.monitor_minutes"))
    kb.button(text=f"Порог стрика: {s.get_int('app.monitoring.alert_streak', 5)}",
              callback_data=SetCB(sec="mon", act="edit", key="app.monitoring.alert_streak"))
    loud = s.get_bool("app.monitoring.service_failure_alert_loud", True)
    kb.button(text=f"{_chk(loud)} Громкий алерт простоя AWG",
              callback_data=SetCB(sec="mon", act="toggle", key="app.monitoring.service_failure_alert_loud"))
    kb.button(text=f"Порог простоя: {s.get_int('app.monitoring.service_failure_alert_minutes', 5)} мин",
              callback_data=SetCB(sec="mon", act="edit", key="app.monitoring.service_failure_alert_minutes"))
    kb.adjust(1)
    kb.row(_back())
    return kb.as_markup()


def settings_backup() -> InlineKeyboardMarkup:
    s = settings
    kb = InlineKeyboardBuilder()
    kb.button(text=f"День автобэкапа: {s.get_int('app.scheduler.backup_day', 1)}",
              callback_data=SetCB(sec="backup", act="edit", key="app.scheduler.backup_day"))
    kb.button(text=f"Час автобэкапа: {s.get_int('app.scheduler.backup_hour', 12)}:00",
              callback_data=SetCB(sec="backup", act="edit", key="app.scheduler.backup_hour"))
    kb.button(text="💾 Сделать бэкап сейчас", callback_data=SetCB(sec="backup", act="do", key="now"))
    kb.adjust(1)
    kb.row(_back())
    return kb.as_markup()


def settings_svc() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Перезапустить AWG", callback_data=SetCB(sec="svc", act="do", key="awg"))
    kb.button(text="🔄 Перезапустить бота", callback_data=SetCB(sec="svc", act="do", key="bot"))
    kb.adjust(1)
    kb.row(_back())
    return kb.as_markup()


def settings_updates(muted: bool) -> InlineKeyboardMarkup:
    """muted — из services (DB-state), не из YAML. never-расписание в UI
    дизейблит тумблер уведомлений (принудительно off)."""
    s = settings
    kb = InlineKeyboardBuilder()
    sched = str(s.get("updates.poll_schedule", "day")).lower()
    never = sched == "never"
    notify_on = (not muted) and not never
    lbl = "Уведомлять об обновлениях" + (" (выкл: расписание «никогда»)" if never else "")
    kb.button(text=f"{_chk(notify_on)} {lbl}",
              callback_data=SetCB(sec="upd", act="toggle", key="notify"))
    kb.adjust(1)
    # пикер расписания
    labels = {"hour": "Каждый час", "day": "Каждый день", "week": "Раз в неделю",
              "month": "Раз в месяц", "never": "Никогда"}
    for opt, text in labels.items():
        mark = "🔘 " if opt == sched else ""
        kb.button(text=f"{mark}{text}", callback_data=SetCB(sec="upd", act="pick", key="sched", val=opt))
    kb.button(text="🔍 Проверить сейчас", callback_data=SetCB(sec="upd", act="do", key="check"))
    kb.adjust(1, 2, 2, 1, 1)
    kb.row(_back())
    return kb.as_markup()


def settings_cancel(sec: str) -> InlineKeyboardMarkup:
    """Отмена ввода значения — вернуться в раздел sec без изменений."""
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f Отмена", callback_data=SetCB(sec=sec))
    return kb.as_markup()
