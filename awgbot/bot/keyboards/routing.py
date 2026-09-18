"""Условная маршрутизация и шлюз — сторона основного бота: профиль, назначение шлюза, настройки."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from awgbot.bot.callbacks import DeviceCB, RoutingCB, SetCB, GwMarkCB, GwSlotCB
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


def gateway_device_actions(dev, back_target: str, slot: int = 0) -> InlineKeyboardMarkup:
    """Карточка устройства-ШЛЮЗА: имя, карточка слота, конфигурация слота,
    выход из роли, пинг последним перед «Назад». Ссылок, лимита, блокировки,
    передачи и удаления нет: всё это у шлюза запрещено сервисами, кнопки бы
    только обещали лишнее."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Имя", callback_data=DeviceCB(action="edit_name", device_id=dev.id))
    kb.button(text="🛰 Карточка шлюза", callback_data=GwSlotCB(action="card", slot=slot))
    kb.button(text="⚙️ Конфигурация шлюза", callback_data=GwSlotCB(action="bundle", slot=slot))
    kb.button(text="🛑 Не шлюз?", callback_data=GwMarkCB(action="remove_ask", device_id=dev.id, slot=slot))
    kb.button(text="📡 Пинг", callback_data=GwSlotCB(action="ping", slot=slot))
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_target))
    kb.adjust(1, 1, 1, 1, 1, 1)
    return kb.as_markup()


def gateway_list(states, *, can_add: bool, failover_on: bool) -> InlineKeyboardMarkup:
    """Список слотов (docs/gateway-failover.md 6.2): по кнопке на слот,
    добавить, тумблер автопереключения (только при двух и более)."""
    kb = InlineKeyboardBuilder()
    rows = []
    for st in states:
        # статус — в инфобоксе; на кнопке только имя и звезда предпочтительного
        dev = st.get("device")
        name = dev.name if dev is not None else f"шлюз {st['gateway'].id}"
        star = "⭐ " if st.get("preferred") else ""
        kb.button(text=f"{star}{name}",
                  callback_data=GwSlotCB(action="card", slot=st["gateway"].id))
        rows.append(1)
    if can_add:
        kb.button(text="➕ Добавить шлюз", callback_data=GwSlotCB(action="add"))
        rows.append(1)
    if len(states) > 1:
        kb.button(text=f"🔁 Автопереключение: {'вкл' if failover_on else 'выкл'}",
                  callback_data=GwSlotCB(action="failover"))
        rows.append(1)
    kb.row(_back("rt"))
    kb.adjust(*rows, 1)
    return kb.as_markup()


def gateway_card(state, *, back_to_list: bool) -> InlineKeyboardMarkup:
    """Карточка слота (6.3): переключение у резервного, галочка
    предпочтительного, конфигурация, подсети, подпись, замена, убрать, пинг
    последним перед «Назад»."""
    gw = state["gateway"]
    kb = InlineKeyboardBuilder()
    rows = []
    if not state.get("active"):
        kb.button(text="▶️ Переключить трафик сюда", callback_data=GwSlotCB(action="switch_ask", slot=gw.id))
        rows.append(1)
    kb.button(text=("✅" if state.get("preferred") else "☑️") + " Предпочтительный при холодном старте",
              callback_data=GwSlotCB(action="pref", slot=gw.id))
    kb.button(text="⚙️ Конфигурация шлюза", callback_data=GwSlotCB(action="bundle", slot=gw.id))
    kb.button(text="🏠 Домашние подсети", callback_data=GwSlotCB(action="home", slot=gw.id))
    kb.button(text="✏️ Подпись", callback_data=GwSlotCB(action="label", slot=gw.id))
    kb.button(text="🔁 Заменить устройство", callback_data=SetCB(sec="rt_gw", act="open", key=str(gw.id)))
    kb.button(text="🛑 Снять шлюз", callback_data=GwSlotCB(action="remove_ask", slot=gw.id))
    kb.button(text="📡 Пинг", callback_data=GwSlotCB(action="ping", slot=gw.id))
    rows += [1, 1, 1, 1, 1, 1, 1]
    back = GwSlotCB(action="list").pack() if back_to_list else SetCB(sec="rt").pack()
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back))
    kb.adjust(*rows, 1)
    return kb.as_markup()


def gateway_choose_kind(has_candidates: bool, slot: int = 0) -> InlineKeyboardMarkup:
    """Назначить машину в слот / заменить: существующее устройство админа или
    новая машина. slot=0 — новый слот."""
    kb = InlineKeyboardBuilder()
    if has_candidates:
        kb.button(text="📱 Из моих устройств", callback_data=GwMarkCB(action="pick_list", slot=slot))
    kb.button(text="➕ Новое устройство", callback_data=GwMarkCB(action="new_ask", slot=slot))
    back = (GwSlotCB(action="card", slot=slot).pack() if slot else SetCB(sec="rt", act="open").pack())
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back))
    kb.adjust(1)
    return kb.as_markup()


def gateway_pick(devices, slot: int = 0) -> InlineKeyboardMarkup:
    """Выбор шлюзового устройства из устройств админа."""
    kb = InlineKeyboardBuilder()
    for d in devices:
        kb.button(text=f"📱 {d.name} ({d.address})",
                  callback_data=GwMarkCB(action="pick", device_id=d.id, slot=slot))
    kb.row(InlineKeyboardButton(text="⬅️ Назад",
                                callback_data=SetCB(sec="rt_gw", act="open", key=str(slot or "")).pack()))
    kb.adjust(*([1] * len(devices)), 1)
    return kb.as_markup()


def gateway_mark_confirm(device_id: int, slot: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛰 Да, это шлюз", callback_data=GwMarkCB(action="mark_yes", device_id=device_id, slot=slot))
    kb.button(text="Отмена", callback_data=SetCB(sec="rt_gw", act="open", key=str(slot or "")))
    kb.adjust(1)
    return kb.as_markup()


def gateway_new_confirm(slot: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Да, новое устройство", callback_data=GwMarkCB(action="new_yes", slot=slot))
    kb.button(text="Отмена", callback_data=SetCB(sec="rt_gw", act="open", key=str(slot or "")))
    kb.adjust(1)
    return kb.as_markup()


def gateway_remove_confirm(slot: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛑 Да, снять шлюз", callback_data=GwSlotCB(action="remove_yes", slot=slot))
    kb.button(text="Отмена", callback_data=(GwSlotCB(action="card", slot=slot) if slot
                                            else SetCB(sec="rt", act="open")))
    kb.adjust(1)
    return kb.as_markup()


def gateway_switch_confirm(slot: int, healthy: bool) -> InlineKeyboardMarkup:
    """Ручное переключение (6.6): «Отмена» первой, как у остальных действий с
    последствиями; у лежащего резерва — «Всё равно переключить»."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data=GwSlotCB(action="card", slot=slot))
    kb.button(text="▶️ Да, переключить" if healthy else "▶️ Всё равно переключить",
              callback_data=GwSlotCB(action="switch_yes", slot=slot))
    kb.adjust(2)
    return kb.as_markup()


def gateway_slot_cancel(slot: int) -> InlineKeyboardMarkup:
    """Отмена ввода (подсети, подпись) — назад в карточку слота."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data=GwSlotCB(action="card", slot=slot))
    return kb.as_markup()


def routing_disable_confirm() -> InlineKeyboardMarkup:
    """Выключить фичу целиком — с подтверждением; «Отмена» первой, как у
    остальных действий с последствиями."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Отмена", callback_data=SetCB(sec="rt", act="open"))
    kb.button(text="🔴 Да, выключить", callback_data=SetCB(sec="rt", act="do", key="off!"))
    kb.adjust(2)
    return kb.as_markup()


def settings_routing(enabled: bool, states=(), *, can_add: bool = True) -> InlineKeyboardMarkup:
    """Раздел «Условная маршрутизация»: выключатель, шлюзы, два подраздела.

    Подразделы показываем ТОЛЬКО при включённой функции: раздавать разрешения
    или обновлять списки для выключенного — приглашение к недоумению «почему у
    клиента не работает, я же разрешил». Без назначенного шлюза вместо
    конфигурации — назначение: конфигурация без шлюза уедет пустой по сути.
    Один слот — кнопка ведёт сразу в его карточку, плюс «➕ Резервный шлюз»;
    два и больше — список.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text=f"{_chk(enabled)} Условная маршрутизация",
              callback_data=SetCB(sec="rt", act="toggle", key="app.routing.enabled"))
    if enabled:
        states = list(states)
        if not states:
            kb.button(text="🛰 Назначить шлюз",
                      callback_data=SetCB(sec="rt_gw", act="open"))
        elif len(states) == 1:
            dev = states[0].get("device")
            name = dev.name if dev is not None else "слот 1"
            kb.button(text=f"🛰 Шлюз: {name}",
                      callback_data=GwSlotCB(action="card", slot=states[0]["gateway"].id))
            if can_add:
                kb.button(text="➕ Резервный шлюз", callback_data=GwSlotCB(action="add"))
        else:
            kb.button(text=f"🛰 Шлюзы: {len(states)}", callback_data=GwSlotCB(action="list"))
        kb.button(text="📋 Списки маршрутизации",
                  callback_data=SetCB(sec="rt_lists", act="open"))
        kb.button(text="👥 Доступность пользователям",
                  callback_data=SetCB(sec="rt_users", act="open"))
        kb.button(text="📡 Мониторинг и резервирование",
                  callback_data=SetCB(sec="rt_mon", act="open"))
    kb.row(_back())
    kb.adjust(1)
    return kb.as_markup()


def settings_routing_monitor(info: dict) -> InlineKeyboardMarkup:
    """Подраздел «Мониторинг и резервирование»: такт зонда, ширина окна и
    порог доступности — пикерами (горячие ключи), тумблер автопереключения."""
    kb = InlineKeyboardBuilder()
    rows = []
    for secs in (30, 45, 60, 90):
        mark = "🔘 " if secs == info["probe_seconds"] else ""
        kb.button(text=f"{mark}такт {secs} с",
                  callback_data=SetCB(sec="rt", act="pick", key="probe", val=str(secs)))
    rows.append(4)
    for n in (5, 10, 20, 30):
        mark = "🔘 " if n == info["window"] else ""
        kb.button(text=f"{mark}окно {n}",
                  callback_data=SetCB(sec="rt", act="pick", key="window", val=str(n)))
    rows.append(4)
    for pct in (25, 50, 75):
        mark = "🔘 " if pct == info["availability"] else ""
        kb.button(text=f"{mark}порог {pct} %",
                  callback_data=SetCB(sec="rt", act="pick", key="avail", val=str(pct)))
    rows.append(3)
    kb.button(text=f"{_chk(info['failover'])} Автопереключение на резерв",
              callback_data=SetCB(sec="rt_mon", act="toggle", key="app.routing.failover.enabled"))
    rows.append(1)
    kb.adjust(*rows)
    kb.row(_back("rt"))
    return kb.as_markup()


def routing_provision() -> InlineKeyboardMarkup:
    """Экран «функция не развёрнута»: одно действие и назад."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Развернуть", callback_data=SetCB(sec="rt", act="do", key="provision"))
    kb.adjust(1)
    kb.row(_back())
    return kb.as_markup()


def settings_routing_bundle(slot: int = 0) -> InlineKeyboardMarkup:
    """Экран перед выпуском конфигурации шлюза: одно действие и отмена.
    Файл уходит в чат с ключом линка внутри — выпуск должен быть осознанным."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Выпустить файл",
              callback_data=SetCB(sec="rt", act="do", key="bundle", val=str(slot or "")))
    kb.button(text="✖️ Отмена", callback_data=(GwSlotCB(action="card", slot=slot).pack() if slot
                                              else SetCB(sec="rt").pack()))
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
