"""Меню клиента и гостя: устройства, карточка устройства, выдача конфигов, помощь, гайды, пауза."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from awgbot.core import blocks as _blocks
from awgbot.bot.callbacks import (
    BlockCB, DelDeviceCB, DeviceCB, FriendCB, GraceCB, GuideCB, HelpCB, Menu, PauseCB,
    RoutingCB)
from awgbot.bot import texts as _texts

from .common import _chk, _btn_suffix, _dev_emoji, append_hide_row, _manual_block_button


# ─────────────────────────────────────────────────────────────────────────────
# Клиентские меню
# ─────────────────────────────────────────────────────────────────────────────

def client_main(has_devices: bool = True, routing_visible: bool = False,
                client_id: int = 0, routing_on: bool = False,
                manage_sub: bool = True) -> InlineKeyboardMarkup:
    """Главное меню клиента. Пункт «Доступ к РФ-сервисам» появляется только после
    того, как админ выдал разрешение: до этого фича невидима, иначе каждый первый
    пойдёт спрашивать, что это за пункт и почему не работает.

    Кружок на кнопке дублирует строку инфобокса — состояние видно и в тексте, и
    на самой кнопке, которой оно меняется."""
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить устройство", callback_data=DeviceCB(action="add"))
    if has_devices:
        kb.button(text="📱 Мои устройства", callback_data=Menu(action="devices"))
    # «Управлять» — у всех, кроме бессрочных: их единственный рычаг — пауза;
    # бессрочной останавливать нечего, экран сугубо информационный
    kb.button(text="⚙️ Управлять подпиской" if manage_sub else "📝 Моя подписка",
              callback_data=Menu(action="info"))
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


def client_devices(devices, held=()) -> InlineKeyboardMarkup:
    """Список своих устройств; следом — чужие, которые профиль держит
    (docs/guest-role.md), с пометкой «от кого». Без кнопки добавления — она
    уже есть в главном меню, дублировать здесь избыточно.

    Значок один — тип устройства. Второй, про онлайн, пробовали и убрали: два
    кружка подряд в каждой строке превращают список в рябь, а ответ «кто в
    сети» есть отдельным экраном.
    """
    kb = InlineKeyboardBuilder()
    for d in devices:
        marker = _blocks.blocked_marker_device(int(d.block_reason), for_admin=False)
        kb.button(text=f"{marker}{_dev_emoji(d)} {d.name}{_btn_suffix(d)}",
                  callback_data=DeviceCB(action="open", device_id=d.id))
    for d in held:
        marker = _blocks.blocked_marker_device(int(d.block_reason), for_admin=False)
        kb.button(text=f"{marker}👤 {d.name} — от {_texts.owner_name(d)}",
                  callback_data=DeviceCB(action="open", device_id=d.id))
    kb.button(text="⬅️ Назад", callback_data=Menu(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def held_device_actions(dev, back_target: str, *, cb_cls=None) -> InlineKeyboardMarkup:
    """Карточка ПЕРЕДАННОГО устройства у держателя (гость или клиент):
    подключение, блокировка, удаление. Имени нет — оно у владельца. cb_cls —
    класс колбэков выдачи: DeviceCB у клиента, FriendCB у гостя."""
    cb_cls = cb_cls or DeviceCB
    kb = InlineKeyboardBuilder()
    kb.button(text="🔌 Данные для подключения",
              callback_data=cb_cls(action="connect_menu", device_id=dev.id))
    bt, bcb = _manual_block_button("dev", dev.id, int(dev.block_reason), for_admin=False)
    kb.button(text=bt, callback_data=bcb)
    kb.button(text="🗑 Удалить", callback_data=DelDeviceCB(device_id=dev.id, stage="ask"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_target))
    kb.adjust(1, 1, 1, 1)
    return kb.as_markup()


def lent_out_device_actions(dev, back_target: str) -> InlineKeyboardMarkup:
    """Карточка переданного устройства у ВЛАДЕЛЬЦА: имя, лимит потребления
    (квота — его, устройство ест её у него) и удаление — остальным управляет
    держатель."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Имя", callback_data=DeviceCB(action="edit_name", device_id=dev.id))
    kb.button(text="📊 Лимит потребления", callback_data=DeviceCB(action="edit_traffic", device_id=dev.id))
    kb.button(text="🗑 Удалить", callback_data=DelDeviceCB(device_id=dev.id, stage="ask"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_target))
    kb.adjust(1, 1, 1, 1)
    return kb.as_markup()


def block_device_confirm(device_id: int, *, guest: bool = False) -> InlineKeyboardMarkup:
    """Подтверждение блокировки своего/переданного устройства (клиент, гость):
    «Отмена» первой, действие с последствиями — не туда, куда палец идёт по
    инерции."""
    kb = InlineKeyboardBuilder()
    back = (FriendCB(action="open", device_id=device_id) if guest
            else DeviceCB(action="open", device_id=device_id))
    kb.button(text="⬅️ Отмена", callback_data=back)
    kb.button(text="🛑 Заблокировать",
              callback_data=BlockCB(target="dev", action="block", ref=device_id, kind="user"))
    kb.adjust(2)
    return kb.as_markup()


def guest_main(*, routing_visible: bool = False, routing_on: bool = False,
               client_id: int = 0) -> InlineKeyboardMarkup:
    """Главное меню гостя (docs/guest-role.md): как клиентское, без добавления
    и подписки. «Мои устройства» — всегда, даже при одном; РФ-доступ — при
    фиче у владельца. Без устройств меню не рисуется вовсе."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Мои устройства", callback_data=FriendCB(action="list"))
    if routing_visible:
        kb.button(text=f"{_chk(routing_on)} Доступ к РФ-сервисам",
                  callback_data=RoutingCB(action="panel", ref=client_id))
    # device_id=0 — «выбери устройство» (при одном — сразу выдача)
    kb.button(text="🔗 Ссылка", callback_data=FriendCB(action="gen_link"))
    kb.button(text="🔳 QR-код", callback_data=FriendCB(action="gen_qr"))
    kb.button(text="📄 Файл", callback_data=FriendCB(action="gen_file"))
    kb.button(text="❓ Помощь с настройкой", callback_data=FriendCB(action="help"))
    kb.adjust(*([1, 1] if routing_visible else [1]), 3, 1)
    return kb.as_markup()


def guest_devices(devices) -> InlineKeyboardMarkup:
    """«Мои устройства» гостя: список, назад — на главный экран гостя."""
    kb = InlineKeyboardBuilder()
    for d in devices:
        marker = _blocks.blocked_marker_device(int(d.block_reason), for_admin=False)
        kb.button(text=f"{marker}📱 {d.name}",
                  callback_data=FriendCB(action="open", device_id=d.id))
    kb.button(text="⬅️ Назад", callback_data=FriendCB(action="refresh"))
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
    # 4) Передать другу / перевыдать инвайт — ТОЛЬКО владельцу. У админа на
    # карточке место передачи занимает «Передать в другой профиль» (две передачи
    # рядом путали, какая куда), а инвайт другу — дело владельца: ссылка уходит
    # в его чат, и хендлер живёт в роутере клиента.
    fstatus = dev.friend_status
    if not is_admin:
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


def connect_method_choice(device_id: int, back_target: str,
                          back_label: str = "⬅️ Назад") -> InlineKeyboardMarkup:
    """«Как планируешь подключить устройство?» — ссылка/QR/файл по одному в
    ряду. Для контекстов с DeviceCB (свои устройства, админ — любое устройство).
    back_label — подпись выхода: после создания устройства возврат ведёт в
    меню, и кнопка так и называется."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Получить ссылку", callback_data=DeviceCB(action="gen_link", device_id=device_id))
    kb.button(text="🔳 Получить QR-код", callback_data=DeviceCB(action="gen_qr", device_id=device_id))
    kb.button(text="📄 Получить файл", callback_data=DeviceCB(action="gen_file", device_id=device_id))
    kb.row(InlineKeyboardButton(text=back_label, callback_data=back_target))
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

PICK_DEVICE_PROMPT = {"gen_link": "Для какого устройства нужна ссылка?",
                      "gen_qr": "Для какого устройства нужен QR-код?",
                      "gen_file": "Для какого устройства нужен файл?"}
GEN_ACTIONS = frozenset(PICK_DEVICE_PROMPT)       # DeviceCB/FriendCB: три вида выдачи


def gen_kind(action: str) -> str:
    """«gen_link» → «link»: вид выдачи для send_device_config/finish_config."""
    return action[len("gen_"):]


def pick_device(devices, action: str, back_cb: str = None) -> InlineKeyboardMarkup:
    """action: gen_link | gen_file | gen_qr — выбор устройства.
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


# ─────────────────────────────────────────────────────────────────────────────
# Меню помощи с настройкой (постактивационное и постоянное)
# ─────────────────────────────────────────────────────────────────────────────


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


def friend_finisher() -> InlineKeyboardMarkup:
    """Завершитель под контентом для гостя — возврат на его главный экран."""
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f В меню", callback_data=FriendCB(action="refresh"))
    return kb.as_markup()


def guest_pick_device(devices, action: str) -> InlineKeyboardMarkup:
    """Выбор устройства гостя под ссылку/QR/файл; назад — на главный экран."""
    kb = InlineKeyboardBuilder()
    for d in devices:
        kb.button(text=f"{d.name}", callback_data=FriendCB(action=action, device_id=d.id))
    kb.button(text="⬅️ Назад", callback_data=FriendCB(action="refresh"))
    kb.adjust(1)
    return kb.as_markup()


def friend_help_back() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f Назад", callback_data=FriendCB(action="help"))
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

def confirm_delete_device(device_id: int, only: bool, *, guest: bool = False) -> InlineKeyboardMarkup:
    """«Отмена» ведёт к карточке ЭТОГО устройства (DeviceCB open; у гостя —
    FriendCB open) — карточка контекстно-корректна для любой роли и точки
    входа. Прежний Menu(devices) у админа уводил в ЕГО СОБСТВЕННЫЙ список
    устройств, даже когда он удалял устройство клиента или бесхозное."""
    kb = InlineKeyboardBuilder()
    if only:
        # усиленное: явная кнопка с признанием риска
        kb.button(text="⚠️ Да, понимаю риск — удалить",
                  callback_data=DelDeviceCB(device_id=device_id, stage="confirm"))
    else:
        kb.button(text="🗑 Да, удалить",
                  callback_data=DelDeviceCB(device_id=device_id, stage="confirm"))
    back = (FriendCB(action="open", device_id=device_id) if guest
            else DeviceCB(action="open", device_id=device_id))
    kb.button(text="Отмена", callback_data=back)
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


def guide_connect_devices(devices, slots, guide: str = "connect") -> InlineKeyboardMarkup:
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


def grace_offer(client_id: int, days: int) -> InlineKeyboardMarkup:
    """Кнопки в уведомлении об истечении (только клиент, годовой период, 1 раз):
    активировать отсрочку или скрыть уведомление (последней строкой — как и
    везде на проактивных уведомлениях)."""
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Продли чуток? 🙏 (+{days} дн.)",
              callback_data=GraceCB(action="take", ref=client_id))
    kb.adjust(1)
    return append_hide_row(kb)


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
