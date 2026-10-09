"""РФ-доступ и шлюзы — сторона основного бота: профиль, экран «Шлюзы», карточка слота, параметры."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from awgbot.bot import ui
from awgbot.bot.callbacks import RoutingCB, SetCB, GwMarkCB, GwSlotCB, Menu
from awgbot.bot import texts as _texts

from .common import _tick, _btn_suffix, select_all_button, confirm, entry_tag
from .settings import _cycle


def routing_panel(client_id: int, devices, *, enabled: int = 0, total: int = 0,
                  n_domains: int = 0, back_target: str, page: int = 0) -> InlineKeyboardMarkup:
    """Раздел «🇷🇺 РФ-доступ» одним экраном: переключатели устройств (свои и
    удерживаемые), «Выбрать все» по правилу массового выбора, добавление
    сайтов и вход в их список. Переданные — строкой в тексте, без кнопки."""
    def _entry(_i, d):
        mark = "✅" if d.routing_on else "☑️"
        held = f" · от профиля {_texts.owner_name(d)}" if d.is_lent else ""
        return (f"{mark} {d.name}{_btn_suffix(d)}{held}", RoutingCB(action="dev", ref=d.id))
    return ui.rows(
        *ui.paged(list(devices), page, static=3 if devices else 2, screen="rtpanel", ref=client_id,
                  back=RoutingCB(action="panel", ref=client_id).pack(), button=_entry),
        select_all_button(enabled, total, RoutingCB(action="all", ref=client_id)) if devices else None,
        [("➕ Сайт", RoutingCB(action="add", ref=client_id, tag="panel")),
         (f"📋 Сайты: {n_domains}" if n_domains else "📋 Сайты", RoutingCB(action="sites", ref=client_id))],
        ui.back(back_target))


def routing_sites(client_id: int, domains: list, page: int = 0) -> InlineKeyboardMarkup:
    """«📋 Сайты»: по кнопке «➖» на адрес (номер — по ПОЛНОМУ списку),
    добавить и очистить, назад — в раздел."""
    return ui.rows(
        *ui.paged(domains, page, static=2, screen="rtsites", ref=client_id,
                  back=RoutingCB(action="sites", ref=client_id).pack(),
                  button=lambda i, dom: (f"➖ {dom}", RoutingCB(action="del", ref=client_id, idx=i, tag=entry_tag(dom)))),
        [("➕ Сайт", RoutingCB(action="add", ref=client_id)),
         ("🗑 Очистить", RoutingCB(action="clear", ref=client_id)) if domains else None],
        ui.back(RoutingCB(action="panel", ref=client_id)))


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
    menu = ui.to_menu(Menu(action="main"))
    if not provisioned:
        return ui.rows(("🚀 Развернуть", SetCB(sec="rt", act="do", key="provision")), menu)
    if not awake:
        return ui.rows(("🔁 Перезапустить сейчас", SetCB(sec="svc", act="do", key="bot!")),
                       ("⬅️ Позже", Menu(action="main")))
    if not enabled:
        return ui.rows(("✅ Включить", SetCB(sec="rt", act="toggle", key="app.routing.enabled")), menu)
    states = list(states)
    body: list = []
    if not states:
        body.append(("🛰 Назначить", SetCB(sec="rt_gw", act="open")))
    else:
        slots = []
        for st in states:
            dev = st.get("device")
            name = dev.name if dev is not None else f"шлюз {st['gateway'].id}"
            star = "⭐ " if st.get("preferred") and len(states) > 1 else ""
            slots.append((f"{star}{name}", GwSlotCB(action="card", slot=st["gateway"].id)))
        if len(states) == 1 and can_add:
            slots.append(("➕ Резерв", GwSlotCB(action="add")))
        body.append(slots)
        standby = next((st for st in states if not st.get("active")), None)
        if standby is not None:
            dev = standby.get("device")
            name = dev.name if dev is not None else "резерв"
            body.append((f"▶️ Переключить на {name}",
                         GwSlotCB(action="switch_ask", slot=standby["gateway"].id, val="l")))
        if len(states) > 1:
            body.append([(f"{_tick(failover_on)} Автопереключение", GwSlotCB(action="failover")),
                         (f"{_tick(peer_nets_on)} Связь подсетей", GwSlotCB(action="peer_ask"))
                         if peer_nets_on is not None else None])
    return ui.rows(
        *body,
        [("👥 Кому доступен", SetCB(sec="rt_users", act="open")), ("⚙️ Параметры", SetCB(sec="rt_params", act="open"))],
        menu)


def gateway_card(state, *, back_to_list: bool, back_main: bool = False) -> InlineKeyboardMarkup:
    """Карточка слота: у резервного — «▶️ Сделать активным» первым рядом;
    [📤 Конфигурация] [📡 Пинг] / [✅ VPN-транзит] [🗺 Подсети] / [❓ Роутер]
    [✏️ Изменить] / [⬅️ Назад]. Без VPN-транзита — без «❓ Роутер»."""
    gw = state["gateway"]
    if back_main:
        back = Menu(action="main")
    else:
        back = GwSlotCB(action="list") if back_to_list else SetCB(sec="rt")
    return ui.rows(
        ("▶️ Сделать активным", GwSlotCB(action="switch_ask", slot=gw.id)) if not state.get("active") else None,
        [("📤 Конфигурация", GwSlotCB(action="bundle", slot=gw.id)), ("📡 Пинг", GwSlotCB(action="ping", slot=gw.id))],
        [(f"{_tick(bool(gw.lan_mode))} VPN-транзит", GwSlotCB(action="lan_ask", slot=gw.id)),
         ("🗺 Подсети", GwSlotCB(action="home", slot=gw.id))],
        [("❓ Роутер", GwSlotCB(action="router", slot=gw.id)) if gw.lan_mode else None,
         ("✏️ Изменить", GwSlotCB(action="edit", slot=gw.id))],
        ui.back(back))


def gateway_edit_kb(state, *, two_slots: bool) -> InlineKeyboardMarkup:
    """«✏️ Изменить» слота: имя устройства и подпись, предпочтительный (при
    двух слотах), замена и снятие."""
    gw, dev = state["gateway"], state.get("device")
    return ui.rows(
        [("✏️ Имя", GwSlotCB(action="name", slot=gw.id)) if dev is not None else None,
         ("✏️ Подпись", GwSlotCB(action="label", slot=gw.id))],
        (f"⭐ При старте: {_tick(bool(state.get('preferred')))}", GwSlotCB(action="pref", slot=gw.id)) if two_slots else None,
        [("🔁 Заменить", SetCB(sec="rt_gw", act="open", key=str(gw.id))), ("🛑 Снять", GwSlotCB(action="remove_ask", slot=gw.id))],
        ui.back(GwSlotCB(action="card", slot=gw.id)))


def gateway_choose_kind(has_candidates: bool, slot: int = 0) -> InlineKeyboardMarkup:
    """Назначить машину в слот / заменить: существующее устройство админа или
    новая машина. slot=0 — новый слот."""
    back = GwSlotCB(action="edit", slot=slot) if slot else SetCB(sec="rt", act="open")
    return ui.rows(
        ("📱 Из моих устройств", GwMarkCB(action="pick_list", slot=slot)) if has_candidates else None,
        ("➕ Новое устройство", GwMarkCB(action="new_ask", slot=slot)),
        ui.back(back))


def gateway_pick(devices, slot: int = 0, page: int = 0) -> InlineKeyboardMarkup:
    """Выбор шлюзового устройства из устройств админа."""
    return ui.rows(
        *ui.paged(devices, page, static=1, screen="gwpick", ref=slot, back=GwMarkCB(action="pick_list", slot=slot).pack(),
                  button=lambda _i, d: (f"📱 {d.name} ({d.address})", GwMarkCB(action="pick", device_id=d.id, slot=slot))),
        ui.back(SetCB(sec="rt_gw", act="open", key=str(slot or ""))))


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
    # цель — в колбэке: «не текущее» на втором нажатии выключало только что включённое
    return confirm(GwSlotCB(action="card", slot=slot), "✅ Включить" if on else "☑️ Выключить",
                   GwSlotCB(action="lan_yes", slot=slot, val="1" if on else "0"), danger=False)


def gateway_peer_confirm(on: bool) -> InlineKeyboardMarkup:
    """Подтверждение «↔️ Связь подсетей»: «Отмена» первой, назад — в «Шлюзы»."""
    return confirm(GwSlotCB(action="list"), "✅ Включить" if on else "☑️ Выключить",
                   GwSlotCB(action="peer_yes", val="1" if on else "0"), danger=False)


def gateway_router_kb(slot: int, tab: str = "mt") -> InlineKeyboardMarkup:
    """Вкладки рецепта: активная — с «✅»; назад — в карточку."""
    from awgbot.bot.texts.routing import ROUTER_TABS
    return ui.rows(
        [(f"✅ {label}" if key == tab else label, GwSlotCB(action="router", slot=slot, val=key)) for key, label in ROUTER_TABS],
        ui.back(GwSlotCB(action="card", slot=slot)))


def gateway_switch_confirm(slot: int, healthy: bool, from_list: bool = False) -> InlineKeyboardMarkup:
    """Ручное переключение: «Отмена» первой — туда, откуда пришли (список или
    карточка); у лежащего резерва — «Всё равно»."""
    src = "l" if from_list else ""
    return confirm(GwSlotCB(action="list") if from_list else GwSlotCB(action="card", slot=slot),
                   "▶️ Переключить" if healthy else "▶️ Всё равно",
                   GwSlotCB(action="switch_yes", slot=slot, val=src), danger=not healthy)



def routing_disable_confirm() -> InlineKeyboardMarkup:
    """Выключить функцию целиком — с подтверждением; назад — в «Параметры»."""
    return confirm(SetCB(sec="rt_params", act="open"), "🔴 Выключить",
                   SetCB(sec="rt", act="do", key="off!"))


def routing_params_kb(info: dict, lists_every: int) -> InlineKeyboardMarkup:
    """«⚙️ Параметры»: такт, окно, порог и период списков — циклами;
    обновить списки; выключить РФ-доступ."""
    return ui.rows(
        [_cycle("rt_params", "app.routing.probe_seconds", f"⏱ Такт: {info['probe_seconds']} с"),
         _cycle("rt_params", "app.routing.failover.window_samples", f"🪟 Окно: {info['window']}")],
        [_cycle("rt_params", "app.routing.failover.min_availability", f"📉 Порог: {info['availability']}%"),
         _cycle("rt_params", "app.routing.lists_refresh_hours", f"🔄 Списки: {lists_every} ч")],
        ("⬇️ Обновить списки", SetCB(sec="rt", act="do", key="lists_refresh")),
        ("🔴 Выключить РФ-доступ", SetCB(sec="rt", act="toggle", key="app.routing.enabled")),
        ui.back(SetCB(sec="rt")))


def routing_provision() -> InlineKeyboardMarkup:
    """Экран «функция не развёрнута»: одно действие и в меню."""
    return gateways_kb((), provisioned=False)


def settings_routing_users(clients=(), page: int = 0) -> InlineKeyboardMarkup:
    """«👥 Кому доступен»: отметки профилей и «Выбрать все» по правилу
    массового выбора; назад — в «Шлюзы»."""
    clients = list(clients)
    return ui.rows(
        *ui.paged(clients, page, static=2 if clients else 1, screen="rtusers", ref=0,
                  back=SetCB(sec="rt_users", act="open").pack(),
                  button=lambda _i, c: (f"{_tick(c.routing_allowed)} {c.name}",
                                        SetCB(sec="rt", act="do", key="allow", val=str(c.id)))),
        select_all_button(sum(1 for c in clients if c.routing_allowed), len(clients),
                          SetCB(sec="rt", act="do", key="allow_all")) if clients else None,
        ui.back(SetCB(sec="rt")))


def bundle_menu_kb(slot: int = 0) -> InlineKeyboardMarkup:
    """Под файлом конфигурации (шифрованным или первого применения): «🛰 В
    карточку» и «⬅️ На главную» — обе убирают файл и сообщение над ним из чата
    (внутри ключ линка) и открывают карточку слота или главную (`bundle_cancel`,
    `bundle_home`). Переслать файл кнопкой Telegram не даёт — пересылают рукой."""
    return ui.rows([("🛰 В карточку", SetCB(sec="rt", act="do", key="bundle_cancel", val=str(slot or ""))),
                    ("⬅️ На главную", SetCB(sec="rt", act="do", key="bundle_home", val=str(slot or "")))])
