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
from awgbot.bot.callbacks import GwCB, HideCB

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

from awgbot.bot.callbacks import (AdminSelfCB, BlockCB, ClientCB, ConfirmCB, DelDeviceCB, DeviceCB,
                       FriendCB, GraceCB, GuideCB, HelpCB, Menu, PauseCB,
                       PeriodCB, ReassignCB, RoutingCB, UpdateCB, SetCB, BroadcastCB)


# ─────────────────────────────────────────────────────────────────────────────
# Клиентские меню
# ─────────────────────────────────────────────────────────────────────────────

def client_main(has_devices: bool = True, routing_visible: bool = False,
                client_id: int = 0, routing_on: bool = False) -> InlineKeyboardMarkup:
    """Главное меню клиента. Пункт «Доступ к РФ-сервисам» появляется только после
    того, как админ выдал разрешение: до этого фича невидима, иначе каждый первый
    пойдёт спрашивать, что это за пункт и почему не работает.

    Кружок на кнопке дублирует строку инфобокса — состояние видно и в тексте, и
    на самой кнопке, которой оно меняется."""
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить устройство", callback_data=DeviceCB(action="add"))
    if has_devices:
        kb.button(text="📱 Мои устройства", callback_data=Menu(action="devices"))
    kb.button(text="⚙️ Управлять подпиской", callback_data=Menu(action="info"))
    if routing_visible:
        kb.button(text=f"{_chk(routing_on)} Доступ к РФ-сервисам",
                  callback_data=RoutingCB(action="panel", ref=client_id))
    if has_devices:
        kb.button(text="🔗 Ссылка", callback_data=Menu(action="gen_link"))
        kb.button(text="🔳 QR-код", callback_data=Menu(action="gen_qr"))
        kb.button(text="📄 Файл", callback_data=Menu(action="gen_file"))
    kb.button(text="❓ Помощь с настройкой", callback_data=HelpCB(platform="root"))
    head = [1, 1, 1] if has_devices else [1, 1]
    if routing_visible:
        head.append(1)
    if has_devices:
        kb.adjust(*head, 3, 1)      # …/ [ссылка|QR|файл] / помощь
    else:
        kb.adjust(*head, 1)
    return kb.as_markup()


def _btn_suffix(dev) -> str:
    """Суффикс имени устройства для КНОПОК (HTML не рендерится): звёздочка у
    устройств, которые бот не создавал. Единый источник для всех списков."""
    return "" if dev.is_managed else " *"


def _dev_emoji(d) -> str:
    """Иконка типа устройства: 📲 передано другу, 📱 обычное."""
    return "📲" if d.friend is not None else "📱"


def client_devices(devices) -> InlineKeyboardMarkup:
    """Список своих устройств. Без кнопки добавления — она уже есть в главном
    меню, дублировать здесь избыточно."""
    kb = InlineKeyboardBuilder()
    for d in devices:
        marker = _blocks.blocked_marker_device(int(d.block_reason), for_admin=False)
        kb.button(text=f"{marker}{_dev_emoji(d)} {d.name}{_btn_suffix(d)}",
                  callback_data=DeviceCB(action="open", device_id=d.id))
    kb.button(text="⬅️ Назад", callback_data=Menu(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def admin_client_back(client_id: int) -> InlineKeyboardMarkup:
    """Единственная кнопка — назад к карточке профиля.

    Для экранов-отбивок, после которых человек должен вернуться туда, откуда
    пришёл, и повторить действие (например, удаление не прошло).
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=ClientCB(action="open", client_id=client_id))
    return kb.as_markup()


def admin_client_device_list(devices, client_id: int) -> InlineKeyboardMarkup:
    """Список устройств КОНКРЕТНОГО клиента (админ смотрит из его карточки).
    Тап открывает карточку устройства (DeviceCB open) — не генерит ссылку сразу.
    «Назад» — к карточке ЭТОГО клиента, не к общему списку клиентов."""
    kb = InlineKeyboardBuilder()
    for d in devices:
        marker = _blocks.blocked_marker_device(int(d.block_reason), for_admin=True)
        kb.button(text=f"{marker}{_dev_emoji(d)} {d.name}", callback_data=DeviceCB(action="open", device_id=d.id))
    kb.button(text="⬅️ Назад", callback_data=ClientCB(action="open", client_id=client_id))
    kb.adjust(1)
    return kb.as_markup()


def routing_devices(client_id: int, devices, *, back_target) -> InlineKeyboardMarkup:
    """Экран устройств профиля: по кнопке на устройство, переключение на месте.

    Один вход вместо тумблеров, рассыпанных по карточкам устройств: всё
    состояние профиля видно разом, а массовое действие лежит тут же первой
    строкой. Карточку устройства не трогаем — она и без того плотная.

    Первая кнопка одна, а не пара «включить»/«выключить»: пока включено хоть
    что-то, осмысленное действие ровно одно — выключить всё. Две кнопки, одна из
    которых всегда холостая, только занимают место.
    """
    kb = InlineKeyboardBuilder()
    # «Выключить все» показываем, только когда включены ВСЕ. При частичном
    # включении полезнее «включить все»: доводить набор до полного — обычное
    # действие, а сбрасывать сделанный выбор — редкое. Та же логика, что на
    # выборе адресатов объявления.
    all_on = bool(devices) and all(d.routing_on for d in devices)
    # Галочки, а не цветные кружки _chk: экран со списком и отметками — это
    # выбор, и он должен читаться так же, как выбор адресатов объявления.
    # Кружки оставлены там, где кнопка показывает СОСТОЯНИЕ чего-то одного.
    kb.button(text="☑️ Выключить все" if all_on else "✅ Включить все",
              callback_data=RoutingCB(action="all", ref=client_id))
    rows = [1]
    for d in devices:
        mark = "✅" if d.routing_on else "☑️"
        kb.button(text=f"{mark} {d.name}{_btn_suffix(d)}",
                  callback_data=RoutingCB(action="dev", ref=d.id))
        rows.append(1)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_target))
    kb.adjust(*rows, 1)
    return kb.as_markup()


def routing_panel(client_id: int, *, master_on: bool, domains: list,
                  enabled: int = 0, total: int = 0,
                  back_target: str) -> InlineKeyboardMarkup:
    """Раздел «Доступ к РФ-сервисам»: вход в устройства и личный список адресов.

    Первая кнопка не переключает, а ОТКРЫВАЕТ список устройств. Раньше она была
    тумблером на весь профиль, и включить режим выборочно было негде.

    Список адресов при выключенном режиме не показываем — он не действует, и
    предлагать редактировать неработающее значит путать."""
    kb = InlineKeyboardBuilder()
    kb.button(text=f"{_chk(master_on)} Устройства: {enabled} из {total}",
              callback_data=RoutingCB(action="devs", ref=client_id))
    rows = [1]
    if master_on:
        kb.button(text="➕ Добавить адреса", callback_data=RoutingCB(action="add", ref=client_id))
        rows.append(1)
        for i, dom in enumerate(domains):
            kb.button(text=f"🗑 {dom}",
                      callback_data=RoutingCB(action="del", ref=client_id, idx=i))
            rows.append(1)
        if domains:
            kb.button(text="🧹 Очистить список",
                      callback_data=RoutingCB(action="clear", ref=client_id))
            rows.append(1)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_target))
    kb.adjust(*rows, 1)
    return kb.as_markup()


def routing_clear_confirm(client_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🧹 Да, очистить", callback_data=RoutingCB(action="clear_yes", ref=client_id))
    kb.button(text="⬅️ Отмена", callback_data=RoutingCB(action="panel", ref=client_id))
    kb.adjust(1, 1)
    return kb.as_markup()


def device_actions(dev, *, is_admin: bool, back_target: str,
                    reassign_label: str = None) -> InlineKeyboardMarkup:
    """Единая карточка устройства — для ЛЮБОГО пути входа (свои устройства,
    устройства конкретного клиента, устройства без клиента). back_target —
    куда ведёт «Назад» (packed callback_data, вычисляется вызывающим кодом из
    принадлежности устройства — не тащим контекст «откуда пришли» через цепочку
    колбэков). reassign_label — текст кнопки привязки/перепривязки (только
    админ; None — кнопки нет, т.е. обычный клиент).

    Созданные ботом: ссылка/QR/файл. Пиры, подхваченные с сервера (без
    приватного ключа), выдать ссылку не могут — WireGuard держит приватный ключ
    только на самом устройстве, а бот его не видел. Такому устройству остаются
    имя, лимит, блокировка и удаление.

    Удаление — ВСЕГДА через подтверждение (DelDeviceCB stage=ask), никогда не
    напрямую: последствия необратимы (ссылка глохнет, друг теряет доступ)."""
    kb = InlineKeyboardBuilder()

    is_bot_device = dev.is_managed
    rows = 0
    # 1) Данные для подключения — только у созданных ботом: у остальных нет
    # приватного ключа, выдавать нечего.
    if is_bot_device:
        kb.button(text="🔌 Данные для подключения",
                  callback_data=DeviceCB(action="connect_menu", device_id=dev.id))
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
    Показываем и устройства без ключа (с суффиксом): клик по ним ведёт не в ошибку,
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


def admin_main(unassigned_count: int, self_has_devices: bool = False,
               routing_visible: bool = False, routing_on: bool = False,
               self_client_id: int = 0) -> InlineKeyboardMarkup:
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
        if routing_visible:
            # админ — такой же пользователь VPN, и режим ему нужен там же, где
            # остальным: рядом со своими устройствами, а не в админских разделах
            kb.button(text=f"{_chk(routing_on)} Доступ к РФ-сервисам",
                      callback_data=RoutingCB(action="panel", ref=self_client_id))
            pattern.append(1)
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


def broadcast_targets(clients, selected) -> InlineKeyboardMarkup:
    """Выбор адресатов: отметки на профилях, «отметить все», «Далее», «Отмена».

    Мультивыбор, а не по одному профилю за раз: объявление обычно касается
    нескольких сразу, и гонять весь путь ввод-превью-отправка по разу на каждого
    значило бы рассылать один и тот же текст руками N раз.

    Набор отмеченного в callback_data не носим — он живёт в FSM-data: лимит
    Telegram 64 байта на строку, а профилей может быть сколько угодно.
    """
    kb = InlineKeyboardBuilder()
    ids = [c.id for c in clients]
    all_on = bool(ids) and all(i in selected for i in ids)
    kb.button(text="☑️ Снять все" if all_on else "✅ Отметить все",
              callback_data=BroadcastCB(action="all"))
    rows = [1]
    for c in clients:
        mark = "✅" if c.id in selected else "☑️"
        kb.button(text=f"{mark} {c.name}",
                  callback_data=BroadcastCB(action="tgl", ref=c.id))
        rows.append(1)
    kb.button(text="\u2b05\ufe0f Отмена", callback_data=BroadcastCB(action="cancel"))
    kb.button(text="➡️ Далее", callback_data=BroadcastCB(action="next"))
    kb.adjust(*rows, 2)
    return kb.as_markup()


def broadcast_cancel() -> InlineKeyboardMarkup:
    """«Отмена» через СВОЙ колбэк, а не прямой навигацией в главное меню.

    Он сбрасывает FSM явно. Прежде отмена вела в меню, чей хендлер чистит
    состояние попутно, — работало, но держалось на побочном эффекте соседа:
    передумавший на шаге ввода админ иначе остался бы в состоянии
    Broadcast.text, и следующее его сообщение стало бы черновиком объявления."""
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f Отмена", callback_data=BroadcastCB(action="cancel"))
    return kb.as_markup()


def broadcast_confirm() -> InlineKeyboardMarkup:
    """Отмена слева, отправка справа — как и на выборе адресатов. Необратимое
    действие не должно стоять там, куда палец идёт по инерции."""
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f Отмена", callback_data=BroadcastCB(action="cancel"))
    kb.button(text="📢 Отправить", callback_data=BroadcastCB(action="send"))
    kb.adjust(2)
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
    """Список клиентов, к которым можно привязать устройство без профиля."""
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
    """Уведомление проактивное — «Скрыть» последней строкой по общему правилу
    (см. HideCB). Способы выдачи — тройкой в один ряд, как в главном меню."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Ссылка", callback_data=DeviceCB(action="gen_link", device_id=device_id))
    kb.button(text="🔳 QR-код", callback_data=DeviceCB(action="gen_qr", device_id=device_id))
    kb.button(text="📄 Файл", callback_data=DeviceCB(action="gen_file", device_id=device_id))
    kb.button(text="❓ Помощь с настройкой", callback_data=HelpCB(platform="root"))
    kb.adjust(3, 1)                 # [ссылка|QR|файл] / помощь / Скрыть
    return append_hide_row(kb)


def unmanaged_device_dialog(device_id: int) -> InlineKeyboardMarkup:
    """Диалог при клике на устройство без ключа в списке «получить ссылку»:
    удалить / назад. Выдать нечего — предлагаем единственный выход."""
    kb = InlineKeyboardBuilder()
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
    return "🟢" if on else "🔴"


def settings_root() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔔 Уведомления", callback_data=SetCB(sec="notify"))
    kb.button(text="💳 Параметры подписок", callback_data=SetCB(sec="subs"))
    if config.ROUTING_ENABLED:
        kb.button(text="🇷🇺 Условная маршрутизация", callback_data=SetCB(sec="rt"))
    kb.button(text="📊 Мониторинг", callback_data=SetCB(sec="mon"))
    kb.button(text="💾 Резервное копирование", callback_data=SetCB(sec="backup"))
    kb.button(text="🔄 Обслуживание", callback_data=SetCB(sec="svc"))
    kb.button(text="⬆️ Обновления бота", callback_data=SetCB(sec="upd"))
    kb.button(text="\u2b05\ufe0f В меню", callback_data=Menu(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def settings_back() -> InlineKeyboardMarkup:
    """Одна кнопка «Назад» — для экранов-отбивок внутри настроек."""
    kb = InlineKeyboardBuilder()
    kb.row(_back())
    return kb.as_markup()


def settings_routing(enabled: bool) -> InlineKeyboardMarkup:
    """Раздел «Условная маршрутизация»: выключатель и три подраздела.

    Подразделы показываем ТОЛЬКО при включённой функции: раздавать разрешения
    или обновлять списки для выключенного — приглашение к недоумению «почему у
    клиента не работает, я же разрешил».
    """
    kb = InlineKeyboardBuilder()
    kb.button(text=f"{_chk(enabled)} Условная маршрутизация",
              callback_data=SetCB(sec="rt", act="toggle", key="app.routing.enabled"))
    if enabled:
        kb.button(text="⚙️ Конфигурация шлюза",
                  callback_data=SetCB(sec="rt", act="do", key="bundle"))
        kb.button(text="📋 Списки маршрутизации",
                  callback_data=SetCB(sec="rt_lists", act="open"))
        kb.button(text="👥 Доступность пользователям",
                  callback_data=SetCB(sec="rt_users", act="open"))
    kb.row(_back())
    kb.adjust(1)
    return kb.as_markup()


def settings_routing_lists(lists_every: int) -> InlineKeyboardMarkup:
    """Подраздел «Списки»: период — пикером (горячий ключ), плюс принудительное
    обновление: ждать до шести часов, когда источник только что починили,
    незачем."""
    kb = InlineKeyboardBuilder()
    for h in (3, 6, 12, 24):
        mark = "🔘 " if h == lists_every else ""
        kb.button(text=f"{mark}{h} ч",
                  callback_data=SetCB(sec="rt", act="pick", key="lists", val=str(h)))
    kb.button(text="🔄 Обновить сейчас",
              callback_data=SetCB(sec="rt", act="do", key="lists_refresh"))
    kb.adjust(4, 1)
    kb.row(_back("rt"))
    return kb.as_markup()


def settings_routing_users(clients=()) -> InlineKeyboardMarkup:
    """Подраздел «Доступность пользователям». Разрешение живёт здесь, а не в
    карточке профиля: это настройка сервиса, а не свойство клиента — все, кому
    выдан доступ, видны одним списком."""
    kb = InlineKeyboardBuilder()
    for c in clients:
        kb.button(text=f"{_chk(c.routing_allowed)} {c.name}",
                  callback_data=SetCB(sec="rt", act="do", key="allow", val=str(c.id)))
    kb.adjust(1)
    kb.row(_back("rt"))
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
        kb.button(text=f"Начало: {s.get_int('quiet_hours.quiet_hours_start', 20)}:00 МСК",
                  callback_data=SetCB(sec="notify", act="edit", key="quiet_hours.quiet_hours_start"))
        kb.button(text=f"Конец: {s.get_int('quiet_hours.quiet_hours_end', 7)}:00 МСК",
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
    for k, label in (("activation", "Активация клиента"), ("grace", "Активация грейс-периода"),
                     ("over_limit", "Превышение лимита потребления"), ("bonus", "Выдача бонусного объёма")):
        on = s.get_bool(f"{ce}.{k}", True)
        kb.button(text=f"{_chk(on)} {label}",
                  callback_data=SetCB(sec="notify", act="toggle", key=f"{ce}.{k}"))
    # раскладка: тихие часы (1) [+ начало/конец (2)] + алерты (1) [+ 3 порога в ряд] +
    # 4 события клиентов — по одной кнопке в ряд для читаемости
    rows = [1] + ([2] if qh else []) + [1] + ([3] if ra else []) + [1, 1, 1, 1]
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
    kb.button(text=f"Продолжительность грейс-периода: {s.get_int('grace.grace_days', 14)}",
              callback_data=SetCB(sec="subs", act="edit", key="grace.grace_days"))
    kb.adjust(1)
    kb.row(_back())
    return kb.as_markup()


def settings_mon() -> InlineKeyboardMarkup:
    s = settings
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Частота опроса: {s.get_int('app.scheduler.monitor_minutes', 3)} мин",
              callback_data=SetCB(sec="mon", act="edit", key="app.scheduler.monitor_minutes"))
    kb.button(text=f"Отсчётов до сработки алерта: {s.get_int('app.monitoring.alert_streak', 5)}",
              callback_data=SetCB(sec="mon", act="edit", key="app.monitoring.alert_streak"))
    loud = s.get_bool("app.monitoring.service_failure_alert_loud", True)
    kb.button(text=f"{_chk(loud)} Алерт простоя AWG со звуком 24/7",
              callback_data=SetCB(sec="mon", act="toggle", key="app.monitoring.service_failure_alert_loud"))
    kb.button(text=f"Порог простоя: {s.get_int('app.monitoring.service_failure_alert_minutes', 5)} мин",
              callback_data=SetCB(sec="mon", act="edit", key="app.monitoring.service_failure_alert_minutes"))
    kb.adjust(1)
    kb.row(_back())
    return kb.as_markup()


def settings_backup() -> InlineKeyboardMarkup:
    s = settings
    kb = InlineKeyboardBuilder()
    kb.button(text=f"📆 День месяца для автобэкапа: {s.get_int('app.scheduler.backup_day', 1)}",
              callback_data=SetCB(sec="backup", act="edit", key="app.scheduler.backup_day"))
    kb.button(text=f"🕘 Время запуска автобэкапа: {s.get_int('app.scheduler.backup_hour', 12)}:00",
              callback_data=SetCB(sec="backup", act="edit", key="app.scheduler.backup_hour"))
    kb.button(text="💾 Создать резервную копию", callback_data=SetCB(sec="backup", act="do", key="now"))
    kb.adjust(1)
    kb.row(_back())
    return kb.as_markup()


def settings_svc(migration: str = "", available: bool = False,
                 orphans: int = 0) -> InlineKeyboardMarkup:
    """Обслуживание. Рычаг переезда показывается, только когда он настроен
    (available) — пустые ключи в app.yaml означают, что фичи нет вовсе, и
    показывать кнопку, которой некуда нажать, незачем.

    Идёт переезд — вместо «начать» два выхода. Оба через подтверждение:
    завершение роняет непереехавших, отмена возвращает всех на старые конфиги.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Перезапустить AWG", callback_data=SetCB(sec="svc", act="do", key="awg"))
    kb.button(text="🔄 Перезапустить бота", callback_data=SetCB(sec="svc", act="do", key="bot"))
    if available:
        if migration:
            kb.button(text="👥 Кто не переехал",
                      callback_data=SetCB(sec="mig", act="do", key="pending"))
            kb.button(text="✅ Завершить переезд",
                      callback_data=SetCB(sec="mig", act="do", key="finish"))
            kb.button(text="↩️ Отменить переезд",
                      callback_data=SetCB(sec="mig", act="do", key="cancel"))
        else:
            kb.button(text="🚚 Начать переезд профилей",
                      callback_data=SetCB(sec="mig", act="do", key="start"))
            if orphans:
                kb.button(text=f"⚠️ Переехавшие после отмены: {orphans}",
                          callback_data=SetCB(sec="mig", act="do", key="orphans"))
    kb.adjust(1)
    kb.row(_back())
    return kb.as_markup()


_MIG_CONFIRM_LABEL = {"start": "🚚 Начать", "finish": "✅ Завершить",
                      "cancel": "↩️ Отменить"}


def migration_confirm(key: str) -> InlineKeyboardMarkup:
    """Подтверждение входа в переезд и обоих выходов. «Не надо» первой: действие
    с последствиями не должно стоять там, куда палец идёт по инерции."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Не надо", callback_data=SetCB(sec="svc", act="open"))
    kb.button(text=_MIG_CONFIRM_LABEL[key],
              callback_data=SetCB(sec="mig", act="do", key=f"{key}!"))
    kb.adjust(2)
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
    labels = {"day": "Каждый день", "week": "Раз в неделю",
              "month": "Раз в месяц", "never": "Никогда"}
    for opt, text in labels.items():
        mark = "🔘 " if opt == sched else ""
        kb.button(text=f"{mark}{text}", callback_data=SetCB(sec="upd", act="pick", key="sched", val=opt))
    kb.button(text="🔍 Проверить сейчас", callback_data=SetCB(sec="upd", act="do", key="check"))
    kb.adjust(1, 2, 2, 1)
    kb.row(_back())
    return kb.as_markup()


def settings_cancel(sec: str) -> InlineKeyboardMarkup:
    """Отмена ввода значения — вернуться в раздел sec без изменений."""
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f Отмена", callback_data=SetCB(sec=sec))
    return kb.as_markup()


def gateway_panel_kb() -> InlineKeyboardMarkup:
    """Панель шлюза. Действия с последствиями (рестарт линка, реассерт) ведут на
    подтверждение — обрыв RF у всех, пусть и на секунды, не должен случаться от
    промаха пальцем. Бандл кнопки не имеет: он приходит файлом в чат."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=GwCB(action="panel"))
    kb.button(text="🩺 Доктор", callback_data=GwCB(action="doctor"))
    kb.button(text="🔁 Рестарт линка", callback_data=GwCB(action="restart"))
    kb.button(text="🛠 Реассерт обвязки", callback_data=GwCB(action="reassert"))
    kb.button(text="⬆️ Обновление агента", callback_data=GwCB(action="updates"))
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def gateway_updates_kb(muted: bool) -> InlineKeyboardMarkup:
    """Раздел обновлений агента — один в один с основным ботом: тумблер
    уведомлений (при расписании «никогда» принудительно выключен), пикер
    расписания с отметкой текущего, ручная проверка."""
    sched = str(settings.get("updates.poll_schedule", "day")).lower()
    never = sched == "never"
    notify_on = (not muted) and not never
    lbl = "Уведомлять об обновлениях" + (" (выкл: расписание «никогда»)" if never else "")
    kb = InlineKeyboardBuilder()
    kb.button(text=f"{_chk(notify_on)} {lbl}", callback_data=GwCB(action="upd_toggle"))
    labels = {"day": "Каждый день", "week": "Раз в неделю",
              "month": "Раз в месяц", "never": "Никогда"}
    for opt, text in labels.items():
        mark = "🔘 " if opt == sched else ""
        kb.button(text=f"{mark}{text}", callback_data=GwCB(action="upd_sched", val=opt))
    kb.button(text="🔍 Проверить сейчас", callback_data=GwCB(action="upd_check"))
    kb.button(text="⬅️ К панели", callback_data=GwCB(action="panel"))
    kb.adjust(1, 2, 2, 1, 1)
    return kb.as_markup()


def gateway_confirm_kb(action: str) -> InlineKeyboardMarkup:
    """«Не надо» первой — необратимое не там, куда палец идёт по инерции."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Не надо", callback_data=GwCB(action="panel"))
    kb.button(text="✅ Выполнить", callback_data=GwCB(action=f"{action}!"))
    kb.adjust(2)
    return kb.as_markup()


def gateway_bundle_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data=GwCB(action="drop"))
    kb.button(text="📦 Применить бандл", callback_data=GwCB(action="apply!"))
    kb.adjust(2)
    return kb.as_markup()


def gateway_back_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ К панели", callback_data=GwCB(action="panel"))
    return kb.as_markup()


def bundle_menu_kb() -> InlineKeyboardMarkup:
    """«В меню» на сообщении с бандлом. Своя кнопка, а не общая с обновлениями:
    та снимает клавиатуру, оставляя текст следом, — а бандл после возврата
    должен ИСЧЕЗНУТЬ из чата: это файл с приватным ключом линка."""
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f В меню", callback_data=SetCB(sec="rt", act="do", key="bundle_menu"))
    return kb.as_markup()


def gateway_update_available_kb() -> InlineKeyboardMarkup:
    """«Есть ступень» у агента: Обновить + к панели. Не update_notify(): та несёт
    «Скрыть» через HideCB, чей хендлер у агента не подключён, — мёртвая кнопка."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬆️ Обновить", callback_data=UpdateCB(action="install"))
    kb.button(text="⬅️ К панели", callback_data=GwCB(action="panel"))
    kb.adjust(1, 1)
    return kb.as_markup()
