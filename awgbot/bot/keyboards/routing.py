"""Условная маршрутизация и шлюз — сторона основного бота: профиль, назначение шлюза, настройки."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from awgbot.bot.callbacks import DeviceCB, RoutingCB, SetCB, GwMarkCB
from awgbot.bot import texts as _texts

from .common import _chk, _tick, _btn_suffix
from .settings import _back


def routing_devices(client_id: int, devices, *, back_target, lent_out=()) -> InlineKeyboardMarkup:
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
        held = f" — от {_texts.owner_name(d)}" if d.is_lent else ""    # чужое, которое держим
        kb.button(text=f"{mark} {d.name}{_btn_suffix(d)}{held}",
                  callback_data=RoutingCB(action="dev", ref=d.id))
        rows.append(1)
    # свои переданные — в самом конце, без переключателя: управляет держатель
    for d in lent_out:
        kb.button(text=f"👤 {d.name} — {_texts.holder_name(d)}",
                  callback_data=RoutingCB(action="lent", ref=d.id))
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
        # Минус, а не корзина: строка убирает ОДНУ запись из списка — то же
        # действие, что «➖» в разделе доступа по SSH. Корзина остаётся там,
        # где сносят всё разом, ниже.
        for i, dom in enumerate(domains):
            kb.button(text=f"➖ {dom}",
                      callback_data=RoutingCB(action="del", ref=client_id, idx=i))
            rows.append(1)
        if domains:
            kb.button(text="🗑 Очистить список",
                      callback_data=RoutingCB(action="clear", ref=client_id))
            rows.append(1)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_target))
    kb.adjust(*rows, 1)
    return kb.as_markup()


def routing_clear_confirm(client_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, очистить", callback_data=RoutingCB(action="clear_yes", ref=client_id))
    kb.button(text="⬅️ Отмена", callback_data=RoutingCB(action="panel", ref=client_id))
    kb.adjust(1, 1)
    return kb.as_markup()


def gateway_device_actions(dev, back_target: str) -> InlineKeyboardMarkup:
    """Карточка ШЛЮЗА: имя, та же кнопка выпуска конфигурации, что в настройках,
    и выход из роли. Ссылок, лимита, блокировки, передачи и удаления нет:
    всё это у шлюза запрещено сервисами, кнопки бы только обещали лишнее."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Имя", callback_data=DeviceCB(action="edit_name", device_id=dev.id))
    kb.button(text="⚙️ Конфигурация шлюза", callback_data=SetCB(sec="rt_bundle", act="open"))
    kb.button(text="🛑 Не шлюз?", callback_data=GwMarkCB(action="remove_ask", device_id=dev.id))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_target))
    kb.adjust(1, 1, 1, 1)
    return kb.as_markup()


def gateway_choose_kind(has_candidates: bool) -> InlineKeyboardMarkup:
    """Назначить/сменить шлюз: существующее устройство админа или новая машина."""
    kb = InlineKeyboardBuilder()
    if has_candidates:
        kb.button(text="📱 Из моих устройств", callback_data=GwMarkCB(action="pick_list"))
    kb.button(text="➕ Новая машина", callback_data=GwMarkCB(action="new_ask"))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=SetCB(sec="rt", act="open").pack()))
    kb.adjust(1)
    return kb.as_markup()


def gateway_pick(devices) -> InlineKeyboardMarkup:
    """Выбор шлюзового устройства из устройств админа."""
    kb = InlineKeyboardBuilder()
    for d in devices:
        kb.button(text=f"📱 {d.name} ({d.address})",
                  callback_data=GwMarkCB(action="pick", device_id=d.id))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=SetCB(sec="rt_gw", act="open").pack()))
    kb.adjust(*([1] * len(devices)), 1)
    return kb.as_markup()


def gateway_mark_confirm(device_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛰 Да, это шлюз", callback_data=GwMarkCB(action="mark_yes", device_id=device_id))
    kb.button(text="Отмена", callback_data=SetCB(sec="rt_gw", act="open"))
    kb.adjust(1)
    return kb.as_markup()


def gateway_new_confirm() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Да, новая машина", callback_data=GwMarkCB(action="new_yes"))
    kb.button(text="Отмена", callback_data=SetCB(sec="rt_gw", act="open"))
    kb.adjust(1)
    return kb.as_markup()


def gateway_remove_confirm() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛑 Да, убрать шлюз", callback_data=GwMarkCB(action="remove_yes"))
    kb.button(text="Отмена", callback_data=SetCB(sec="rt", act="open"))
    kb.adjust(1)
    return kb.as_markup()


def routing_disable_confirm() -> InlineKeyboardMarkup:
    """Выключить фичу целиком — с подтверждением; «Отмена» первой, как у
    остальных действий с последствиями."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data=SetCB(sec="rt", act="open"))
    kb.button(text="🔴 Да, выключить", callback_data=SetCB(sec="rt", act="do", key="off!"))
    kb.adjust(2)
    return kb.as_markup()


def settings_routing(enabled: bool, has_gateway: bool = True) -> InlineKeyboardMarkup:
    """Раздел «Условная маршрутизация»: выключатель и три подраздела.

    Подразделы показываем ТОЛЬКО при включённой функции: раздавать разрешения
    или обновлять списки для выключенного — приглашение к недоумению «почему у
    клиента не работает, я же разрешил». Без назначенного шлюза вместо
    конфигурации — назначение: конфигурация без шлюза уедет пустой по сути.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text=f"{_chk(enabled)} Условная маршрутизация",
              callback_data=SetCB(sec="rt", act="toggle", key="app.routing.enabled"))
    if enabled:
        if has_gateway:
            kb.button(text="⚙️ Конфигурация шлюза",
                      callback_data=SetCB(sec="rt_bundle", act="open"))
            kb.button(text="🔁 Сменить шлюз",
                      callback_data=SetCB(sec="rt_gw", act="open"))
            kb.button(text="🛑 Убрать шлюз",
                      callback_data=GwMarkCB(action="remove_ask"))
        else:
            kb.button(text="🛰 Назначить шлюз",
                      callback_data=SetCB(sec="rt_gw", act="open"))
        kb.button(text="📋 Списки маршрутизации",
                  callback_data=SetCB(sec="rt_lists", act="open"))
        kb.button(text="👥 Доступность пользователям",
                  callback_data=SetCB(sec="rt_users", act="open"))
    kb.row(_back())
    kb.adjust(1)
    return kb.as_markup()


def routing_provision() -> InlineKeyboardMarkup:
    """Экран «функция не развёрнута»: одно действие и назад."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Развернуть", callback_data=SetCB(sec="rt", act="do", key="provision"))
    kb.adjust(1)
    kb.row(_back())
    return kb.as_markup()


def settings_routing_bundle() -> InlineKeyboardMarkup:
    """Экран перед выпуском конфигурации шлюза: одно действие и отмена.
    Файл уходит в чат с ключом линка внутри — выпуск должен быть осознанным."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Выпустить файл",
              callback_data=SetCB(sec="rt", act="do", key="bundle"))
    kb.button(text="✖️ Отмена", callback_data=SetCB(sec="rt").pack())
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
        kb.button(text=f"{_tick(c.routing_allowed)} {c.name}",
                  callback_data=SetCB(sec="rt", act="do", key="allow", val=str(c.id)))
    kb.adjust(1)
    kb.row(_back("rt"))
    return kb.as_markup()


def bundle_menu_kb() -> InlineKeyboardMarkup:
    """«В меню» на сообщении с бандлом. Своя кнопка, а не общая с обновлениями:
    та снимает клавиатуру, оставляя текст следом, — а бандл после возврата
    должен ИСЧЕЗНУТЬ из чата: это файл с приватным ключом линка."""
    kb = InlineKeyboardBuilder()
    kb.button(text="\u2b05\ufe0f В меню", callback_data=SetCB(sec="rt", act="do", key="bundle_menu"))
    return kb.as_markup()
