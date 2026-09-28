"""РФ-доступ и шлюзы — сторона основного бота: профиль, экран «Шлюзы», карточка слота, параметры."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from awgbot.bot.callbacks import RoutingCB, SetCB, GwMarkCB, GwSlotCB, Menu
from awgbot.bot import texts as _texts

from .common import _tick, _btn_suffix, page_slice, page_nav, select_all_button, confirm
from .settings import _cycle


def routing_panel(client_id: int, devices, *, lent_out=(), enabled: int = 0, total: int = 0,
                  n_domains: int = 0, back_target: str, page: int = 0, **_legacy) -> InlineKeyboardMarkup:
    """Раздел «🇷🇺 РФ-доступ» одним экраном: переключатели устройств (свои и
    удерживаемые), «Выбрать все» по правилу массового выбора, добавление
    сайтов и вход в их список. Переданные — строкой в тексте, без кнопки."""
    kb = InlineKeyboardBuilder()
    rows = []
    chunk, page, prev, nxt = page_slice(list(devices), page, static=4 if devices else 3)
    for _i, d in chunk:
        mark = "✅" if d.routing_on else "☑️"
        held = f" · от профиля {_texts.owner_name(d)}" if d.is_lent else ""
        kb.button(text=f"{mark} {d.name}{_btn_suffix(d)}{held}",
                  callback_data=RoutingCB(action="dev", ref=d.id))
        rows.append(1)
    nav = page_nav(kb, "rtpanel", client_id, page, prev, nxt,
                   RoutingCB(action="panel", ref=client_id).pack())
    if nav:
        rows.append(nav)
    if devices:
        kb.add(select_all_button(enabled, total, RoutingCB(action="all", ref=client_id)))
        rows.append(1)
    kb.button(text="➕ Сайт", callback_data=RoutingCB(action="add", ref=client_id))
    kb.button(text=f"📋 Сайты: {n_domains}" if n_domains else "📋 Сайты",
              callback_data=RoutingCB(action="sites", ref=client_id))
    rows.append(2)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back_target))
    kb.adjust(*rows, 1)
    return kb.as_markup()


def routing_sites(client_id: int, domains: list, page: int = 0) -> InlineKeyboardMarkup:
    """«📋 Сайты»: по кнопке «➖» на адрес (номер — по ПОЛНОМУ списку),
    добавить и очистить, назад — в раздел."""
    kb = InlineKeyboardBuilder()
    rows = []
    chunk, page, prev, nxt = page_slice(domains, page, static=3 if domains else 2)
    for i, dom in chunk:
        kb.button(text=f"➖ {dom}", callback_data=RoutingCB(action="del", ref=client_id, idx=i))
        rows.append(1)
    nav = page_nav(kb, "rtsites", client_id, page, prev, nxt,
                   RoutingCB(action="sites", ref=client_id).pack())
    if nav:
        rows.append(nav)
    kb.button(text="➕ Сайт", callback_data=RoutingCB(action="add", ref=client_id))
    if domains:
        kb.button(text="🗑 Очистить", callback_data=RoutingCB(action="clear", ref=client_id))
        rows.append(2)
    else:
        rows.append(1)
    kb.button(text="⬅️ Назад", callback_data=RoutingCB(action="panel", ref=client_id))
    kb.adjust(*rows, 1)
    return kb.as_markup()


def routing_clear_confirm(client_id: int) -> InlineKeyboardMarkup:
    return confirm(RoutingCB(action="sites", ref=client_id), "🗑 Удалить все",
                   RoutingCB(action="clear_yes", ref=client_id))


def gateways_kb(states, *, enabled: bool = True, provisioned: bool = True, awake: bool = True,
                can_add: bool = True, failover_on: bool = True,
                peer_nets_on: bool | None = None) -> InlineKeyboardMarkup:
    """Экран «🛰 Шлюзы» с главной: не развёрнуто — «🚀 Развернуть»; спит до
    перезапуска — только выход; выключено — «✅ Включить»; включено — слоты
    кнопками (⭐ у предпочтительного), переключение на резерв, тумблеры
    автопереключения и связи подсетей (при двух слотах), «Кому доступен» и
    «Параметры»."""
    kb = InlineKeyboardBuilder()
    rows = []
    if not provisioned:
        kb.button(text="🚀 Развернуть", callback_data=SetCB(sec="rt", act="do", key="provision"))
        rows.append(1)
    elif not awake:
        pass
    elif not enabled:
        kb.button(text="✅ Включить", callback_data=SetCB(sec="rt", act="toggle", key="app.routing.enabled"))
        rows.append(1)
    else:
        states = list(states)
        if not states:
            kb.button(text="🛰 Назначить", callback_data=SetCB(sec="rt_gw", act="open"))
            rows.append(1)
        else:
            for st in states:
                dev = st.get("device")
                name = dev.name if dev is not None else f"шлюз {st['gateway'].id}"
                star = "⭐ " if st.get("preferred") and len(states) > 1 else ""
                kb.button(text=f"{star}{name}", callback_data=GwSlotCB(action="card", slot=st["gateway"].id))
            if len(states) == 1 and can_add:
                kb.button(text="➕ Резерв", callback_data=GwSlotCB(action="add"))
                rows.append(2)
            else:
                rows.append(len(states))
            standby = next((st for st in states if not st.get("active")), None)
            if standby is not None:
                dev = standby.get("device")
                name = dev.name if dev is not None else "резерв"
                kb.button(text=f"▶️ Переключить на {name}",
                          callback_data=GwSlotCB(action="switch_ask", slot=standby["gateway"].id, val="l"))
                rows.append(1)
            if len(states) > 1:
                kb.button(text=f"{_tick(failover_on)} Автопереключение", callback_data=GwSlotCB(action="failover"))
                if peer_nets_on is not None:
                    kb.button(text=f"{_tick(peer_nets_on)} Связь подсетей", callback_data=GwSlotCB(action="peer_ask"))
                    rows.append(2)
                else:
                    rows.append(1)
        kb.button(text="👥 Кому доступен", callback_data=SetCB(sec="rt_users", act="open"))
        kb.button(text="⚙️ Параметры", callback_data=SetCB(sec="rt_params", act="open"))
        rows.append(2)
    kb.button(text="⬅️ В меню", callback_data=Menu(action="main"))
    rows.append(1)
    kb.adjust(*rows)
    return kb.as_markup()


def gateway_list(states, *, can_add: bool, failover_on: bool,
                 peer_nets_on: bool | None = None) -> InlineKeyboardMarkup:
    """Прежнее имя экрана «Шлюзы»."""
    return gateways_kb(states, can_add=can_add, failover_on=failover_on, peer_nets_on=peer_nets_on)


def settings_routing(enabled: bool, states=(), *, can_add: bool = True) -> InlineKeyboardMarkup:
    """Прежний раздел настроек — теперь экран «Шлюзы»."""
    return gateways_kb(states, enabled=enabled, can_add=can_add)


def gateway_card(state, *, back_to_list: bool, back_home: bool = False) -> InlineKeyboardMarkup:
    """Карточка слота: у резервного — «▶️ Сделать активным» первым рядом;
    [📤 Конфигурация] [📡 Пинг] / [✅ VPN-транзит] [🗺 Подсети] / [❓ Роутер]
    [✏️ Изменить] / [⬅️ Назад]. Без VPN-транзита — без «❓ Роутер»."""
    gw = state["gateway"]
    kb = InlineKeyboardBuilder()
    rows = []
    if not state.get("active"):
        kb.button(text="▶️ Сделать активным", callback_data=GwSlotCB(action="switch_ask", slot=gw.id))
        rows.append(1)
    kb.button(text="📤 Конфигурация", callback_data=GwSlotCB(action="bundle", slot=gw.id))
    kb.button(text="📡 Пинг", callback_data=GwSlotCB(action="ping", slot=gw.id))
    kb.button(text=f"{_tick(bool(gw.lan_mode))} VPN-транзит", callback_data=GwSlotCB(action="lan_ask", slot=gw.id))
    kb.button(text="🗺 Подсети", callback_data=GwSlotCB(action="home", slot=gw.id))
    rows += [2, 2]
    if gw.lan_mode:
        kb.button(text="❓ Роутер", callback_data=GwSlotCB(action="router", slot=gw.id))
        kb.button(text="✏️ Изменить", callback_data=GwSlotCB(action="edit", slot=gw.id))
        rows.append(2)
    else:
        kb.button(text="✏️ Изменить", callback_data=GwSlotCB(action="edit", slot=gw.id))
        rows.append(1)
    if back_home:
        back = Menu(action="main").pack()
    else:
        back = GwSlotCB(action="list").pack() if back_to_list else SetCB(sec="rt").pack()
    kb.button(text="⬅️ Назад", callback_data=back)
    rows.append(1)
    kb.adjust(*rows)
    return kb.as_markup()


def gateway_edit_kb(state, *, two_slots: bool) -> InlineKeyboardMarkup:
    """«✏️ Изменить» слота: имя устройства и подпись, предпочтительный (при
    двух слотах), замена и снятие."""
    gw, dev = state["gateway"], state.get("device")
    kb = InlineKeyboardBuilder()
    rows = []
    if dev is not None:
        kb.button(text="✏️ Имя", callback_data=GwSlotCB(action="name", slot=gw.id))
        kb.button(text="✏️ Подпись", callback_data=GwSlotCB(action="label", slot=gw.id))
        rows.append(2)
    else:
        kb.button(text="✏️ Подпись", callback_data=GwSlotCB(action="label", slot=gw.id))
        rows.append(1)
    if two_slots:
        kb.button(text=f"⭐ При старте: {_tick(bool(state.get('preferred')))}",
                  callback_data=GwSlotCB(action="pref", slot=gw.id))
        rows.append(1)
    kb.button(text="🔁 Заменить", callback_data=SetCB(sec="rt_gw", act="open", key=str(gw.id)))
    kb.button(text="🛑 Снять", callback_data=GwSlotCB(action="remove_ask", slot=gw.id))
    kb.button(text="⬅️ Назад", callback_data=GwSlotCB(action="card", slot=gw.id))
    rows += [2, 1]
    kb.adjust(*rows)
    return kb.as_markup()


def gateway_choose_kind(has_candidates: bool, slot: int = 0) -> InlineKeyboardMarkup:
    """Назначить машину в слот / заменить: существующее устройство админа или
    новая машина. slot=0 — новый слот."""
    kb = InlineKeyboardBuilder()
    if has_candidates:
        kb.button(text="📱 Из моих устройств", callback_data=GwMarkCB(action="pick_list", slot=slot))
    kb.button(text="➕ Новое устройство", callback_data=GwMarkCB(action="new_ask", slot=slot))
    back = (GwSlotCB(action="edit", slot=slot).pack() if slot else SetCB(sec="rt", act="open").pack())
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=back))
    kb.adjust(1)
    return kb.as_markup()


def gateway_pick(devices, slot: int = 0, page: int = 0) -> InlineKeyboardMarkup:
    """Выбор шлюзового устройства из устройств админа."""
    kb = InlineKeyboardBuilder()
    chunk, page, prev, nxt = page_slice(devices, page, static=1)
    for _i, d in chunk:
        kb.button(text=f"📱 {d.name} ({d.address})",
                  callback_data=GwMarkCB(action="pick", device_id=d.id, slot=slot))
    nav = page_nav(kb, "gwpick", slot, page, prev, nxt, GwMarkCB(action="pick_list", slot=slot).pack())
    kb.adjust(*([1] * len(chunk)), *([nav] if nav else []))
    kb.row(InlineKeyboardButton(text="⬅️ Назад",
                                callback_data=SetCB(sec="rt_gw", act="open", key=str(slot or "")).pack()))
    return kb.as_markup()


def gateway_mark_confirm(device_id: int, slot: int = 0) -> InlineKeyboardMarkup:
    return confirm(SetCB(sec="rt_gw", act="open", key=str(slot or "")), "🛰 Назначить",
                   GwMarkCB(action="mark_yes", device_id=device_id, slot=slot), danger=False)


def gateway_new_confirm(slot: int = 0) -> InlineKeyboardMarkup:
    return confirm(SetCB(sec="rt_gw", act="open", key=str(slot or "")), "🔁 Заменить",
                   GwMarkCB(action="new_yes", slot=slot), danger=False)


def gateway_remove_confirm(slot: int = 0) -> InlineKeyboardMarkup:
    cancel = GwSlotCB(action="edit", slot=slot) if slot else SetCB(sec="rt", act="open")
    return confirm(cancel, "🛑 Снять", GwSlotCB(action="remove_yes", slot=slot))


def gateway_lan_confirm(slot: int, on: bool) -> InlineKeyboardMarkup:
    """Подтверждение «🔀 VPN-транзит»: «Отмена» первой."""
    return confirm(GwSlotCB(action="card", slot=slot), "✅ Включить" if on else "☑️ Выключить",
                   GwSlotCB(action="lan_yes", slot=slot), danger=False)


def gateway_peer_confirm(on: bool) -> InlineKeyboardMarkup:
    """Подтверждение «↔️ Связь подсетей»: «Отмена» первой, назад — в «Шлюзы»."""
    return confirm(GwSlotCB(action="list"), "✅ Включить" if on else "☑️ Выключить",
                   GwSlotCB(action="peer_yes"), danger=False)


def gateway_router_kb(slot: int, tab: str = "mt") -> InlineKeyboardMarkup:
    """Вкладки рецепта: активная — с «✅»; назад — в карточку."""
    from awgbot.bot.texts.routing import ROUTER_TABS
    kb = InlineKeyboardBuilder()
    for key, label in ROUTER_TABS:
        kb.button(text=(f"✅ {label}" if key == tab else label),
                  callback_data=GwSlotCB(action="router", slot=slot, val=key))
    kb.button(text="⬅️ Назад", callback_data=GwSlotCB(action="card", slot=slot))
    kb.adjust(len(ROUTER_TABS), 1)
    return kb.as_markup()


def gateway_router_back(slot: int) -> InlineKeyboardMarkup:
    return gateway_router_kb(slot)


def gateway_switch_confirm(slot: int, healthy: bool, from_list: bool = False) -> InlineKeyboardMarkup:
    """Ручное переключение: «Отмена» первой — туда, откуда пришли (список или
    карточка); у лежащего резерва — «Всё равно»."""
    src = "l" if from_list else ""
    return confirm(GwSlotCB(action="list") if from_list else GwSlotCB(action="card", slot=slot),
                   "▶️ Переключить" if healthy else "▶️ Всё равно",
                   GwSlotCB(action="switch_yes", slot=slot, val=src), danger=not healthy)


def gateway_slot_cancel(slot: int, to_edit: bool = False) -> InlineKeyboardMarkup:
    """Отмена ввода — назад в карточку слота (подсети) или в «✏️ Изменить»
    (имя, подпись)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data=GwSlotCB(action="edit" if to_edit else "card", slot=slot))
    return kb.as_markup()


def routing_disable_confirm() -> InlineKeyboardMarkup:
    """Выключить функцию целиком — с подтверждением; назад — в «Параметры»."""
    return confirm(SetCB(sec="rt_params", act="open"), "🔴 Выключить",
                   SetCB(sec="rt", act="do", key="off!"))


def routing_params_kb(info: dict, lists_every: int) -> InlineKeyboardMarkup:
    """«⚙️ Параметры»: такт, окно, порог и период списков — циклами;
    обновить списки; выключить РФ-доступ."""
    kb = InlineKeyboardBuilder()
    kb.add(_cycle("rt_params", "app.routing.probe_seconds", f"⏱ Такт: {info['probe_seconds']} с"))
    kb.add(_cycle("rt_params", "app.routing.failover.window_samples", f"🪟 Окно: {info['window']}"))
    kb.add(_cycle("rt_params", "app.routing.failover.min_availability", f"📉 Порог: {info['availability']}%"))
    kb.add(_cycle("rt_params", "app.routing.lists_refresh_hours", f"🔄 Списки: {lists_every} ч"))
    kb.button(text="⬇️ Обновить списки", callback_data=SetCB(sec="rt", act="do", key="lists_refresh"))
    kb.button(text="🔴 Выключить РФ-доступ", callback_data=SetCB(sec="rt", act="toggle", key="app.routing.enabled"))
    kb.button(text="⬅️ Назад", callback_data=SetCB(sec="rt"))
    kb.adjust(2, 2, 1, 1, 1)
    return kb.as_markup()


def settings_routing_monitor(info: dict) -> InlineKeyboardMarkup:
    """Прежний подраздел — теперь «⚙️ Параметры»."""
    from awgbot.core import settings as _settings
    return routing_params_kb(info, int(_settings.get("app.routing.lists_refresh_hours", 6)))


def settings_routing_lists(lists_every: int) -> InlineKeyboardMarkup:
    """Прежний подраздел — теперь «⚙️ Параметры»."""
    from awgbot.core import settings as _settings
    info = {"probe_seconds": _settings.get_int("app.routing.probe_seconds", 30),
            "window": _settings.get_int("app.routing.failover.window_samples", 10),
            "availability": _settings.get_int("app.routing.failover.min_availability", 50)}
    return routing_params_kb(info, lists_every)


def routing_provision() -> InlineKeyboardMarkup:
    """Экран «функция не развёрнута»: одно действие и в меню."""
    return gateways_kb((), provisioned=False)


def settings_routing_users(clients=(), page: int = 0) -> InlineKeyboardMarkup:
    """«👥 Кому доступен»: отметки профилей и «Выбрать все» по правилу
    массового выбора; назад — в «Шлюзы»."""
    kb = InlineKeyboardBuilder()
    clients = list(clients)
    chunk, page, prev, nxt = page_slice(clients, page, static=2 if clients else 1)
    for _i, c in chunk:
        kb.button(text=f"{_tick(c.routing_allowed)} {c.name}",
                  callback_data=SetCB(sec="rt", act="do", key="allow", val=str(c.id)))
    nav = page_nav(kb, "rtusers", 0, page, prev, nxt, SetCB(sec="rt_users", act="open").pack())
    rows = [*([1] * len(chunk)), *([nav] if nav else [])]
    if clients:
        kb.add(select_all_button(sum(1 for c in clients if c.routing_allowed), len(clients),
                                 SetCB(sec="rt", act="do", key="allow_all")))
        rows.append(1)
    kb.button(text="⬅️ Назад", callback_data=SetCB(sec="rt"))
    rows.append(1)
    kb.adjust(*rows)
    return kb.as_markup()


def bundle_menu_kb(slot: int = 0) -> InlineKeyboardMarkup:
    """«В меню» на сообщении с файлом конфигурации (шифрованным или первого
    применения): файл и сообщение над ним уходят из чата — внутри ключ линка,
    — и открывается карточка слота, из которой файл выпускали (`bundle_cancel`).
    Своя кнопка, а не общая с обновлениями: та снимает клавиатуру, оставляя текст."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ В меню",
              callback_data=SetCB(sec="rt", act="do", key="bundle_cancel", val=str(slot or "")))
    return kb.as_markup()
