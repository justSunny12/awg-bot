"""РФ-доступ и шлюзы — сторона основного бота: профиль, экран «Шлюзы», карточка слота, параметры."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from awgbot.bot import ui
from awgbot.bot.callbacks import RoutingCB, SetCB, GwMarkCB, GwSlotCB, Menu

from .common import _tick, _btn_suffix, select_all_button, apply_row, confirm, entry_tag
from .settings import _cycle


def routing_panel(client_id: int, devices, *, marks: dict, dirty: bool = False,
                  n_domains: int = 0, back_target: str, page: int = 0,
                  back_label: str = ui.BACK_LABEL) -> InlineKeyboardMarkup:
    """Раздел «🇷🇺 РФ-доступ» одним экраном: отметки устройств (свои и
    удерживаемые) — из marks {id: отмечено}, это выбор, а не состояние; «Все
    устройства» по правилу массового выбора; добавление сайтов и вход в их
    список; последним рядом — «✅ Применить» (только при отличии от
    сохранённого, dirty) и выход (на главную — «⬅️ В меню», back_label).
    Переданные — строкой в тексте, без кнопки;
    у гостя хвоста «от профиля» на кнопках нет — профиль в заголовке."""
    def _entry(_i, d):
        mark = "✅" if marks.get(d.id, bool(d.routing_on)) else "☑️"
        return (f"{mark} {d.name}{_btn_suffix(d)}", RoutingCB(action="dev", ref=d.id))
    devices = list(devices)
    return ui.rows(
        *ui.paged(devices, page, static=3 if devices else 2, screen="rtpanel", ref=client_id,
                  back=RoutingCB(action="page", ref=client_id).pack(), button=_entry),
        select_all_button(sum(1 for v in marks.values() if v), len(marks),
                          RoutingCB(action="all", ref=client_id), "Все устройства") if devices else None,
        [("➕ Сайт", RoutingCB(action="add", ref=client_id, tag="panel")),
         (f"📋 Свои сайты: {n_domains}" if n_domains else "📋 Свои сайты", RoutingCB(action="sites", ref=client_id))],
        apply_row(dirty, RoutingCB(action="apply", ref=client_id), back_target, back_label))


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


BTN_MAX = 18      # подпись кнопки в ряду из двух — до 18 знаков
SWITCH_NAME_MAX = 14   # имя на «▶️ Переключить на …» (одна в ряду, ориентир 30 знаков)


def _fit(label: str, limit: int = BTN_MAX) -> str:
    """Подпись длиннее предела — обрезка с «…» (имя и подпись слота задаёт человек)."""
    return label if len(label) <= limit else label[:limit - 1].rstrip() + "…"


def slot_button_label(name: str, label: str = "", *, star: bool = False, fit: bool = True,
                      limit: int = BTN_MAX) -> str:
    """Кнопка слота: «⭐ Имя (подпись)». Длиннее предела — режется имя, подпись
    остаётся («Raspberr… (дача)»): подпись и есть то, чем слоты с похожими
    именами различают; подпись длиннее половины предела — режется всё целиком.
    fit=False — кнопка одна в ряду, места хватает, не режем."""
    head = "⭐ " if star else ""
    tail = f" ({label})" if label else ""
    full = f"{head}{name}{tail}"
    if not fit or len(full) <= limit:
        return full
    if label and len(label) <= limit // 2:
        room = limit - len(head) - len(tail) - 1
        return f"{head}{name[:room].rstrip()}…{tail}"
    return _fit(full, limit)


def gateways_kb(states, *, enabled: bool = True, provisioned: bool = True, awake: bool = True,
                can_add: bool = True, failover_on: bool = True,
                smb_on: bool | None = None) -> InlineKeyboardMarkup:
    """Экран «🛰 Шлюзы» с главной: не развёрнуто — «🚀 Развернуть»; спит до
    перезапуска — только выход; выключено — «✅ Включить»; включено — слоты
    кнопками (⭐ у предпочтительного), переключение на резерв, тумблер
    автопереключения (при двух слотах), ниже «SMB в туннеле» (smb_on не None
    — есть слоты), «Кому доступен» и «Параметры». Кнопка слота — имя и подпись
    («Pi4 (ЕК-22П10)»), в ряду из двух длиннее 18 знаков — режется имя, подпись
    остаётся (slot_button_label). Связь подсетей — часть
    VPN-транзита, своей кнопки у неё нет."""
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
        two_up = len(states) > 1 or can_add       # ряд из двух и больше — подписи режутся
        for st in states:
            dev, gw = st.get("device"), st["gateway"]
            name = dev.name if dev is not None else f"шлюз {gw.id}"
            slots.append((slot_button_label(name, gw.label or "", star=bool(st.get("preferred")) and len(states) > 1,
                                            fit=two_up),
                          GwSlotCB(action="card", slot=gw.id)))
        if len(states) == 1 and can_add:
            slots.append(("➕ Резерв", GwSlotCB(action="add")))
        body.append(slots)
        standby = next((st for st in states if not st.get("active")), None)
        # лежащий резерв (🔴) не предлагаем: переключение на него — РФ-доступ без шлюза
        if standby is not None and not standby.get("unavailable"):
            dev = standby.get("device")
            name = dev.name if dev is not None else "резерв"
            body.append((f"▶️ Переключить на {_fit(name, SWITCH_NAME_MAX)}",
                         GwSlotCB(action="switch_ask", slot=standby["gateway"].id, val="l")))
        if len(states) > 1:
            body.append((f"{_tick(failover_on)} Автопереключение", GwSlotCB(action="failover")))
    if smb_on is not None:
        body.append((f"{_tick(smb_on)} SMB в туннеле", GwSlotCB(action="smb_ask")))
    return ui.rows(
        *body,
        [("👥 Кому доступен", SetCB(sec="rt_users", act="open")), ("⚙️ Параметры", SetCB(sec="rt_params", act="open"))],
        menu)


def gateway_card(state, *, back_to_list: bool, back_main: bool = False) -> InlineKeyboardMarkup:
    """Карточка слота: у резервного — «▶️ Сделать активным» первым рядом;
    [📤 Конфигурация] [📡 Пинг] / [✅ VPN-транзит] [🗺 Подсети] / [🛜 Роутер]
    [✏️ Изменить] / [⬅️ Назад]. Без VPN-транзита — без «🛜 Роутер»; агенту с
    документом конфигурации файл не нужен — без «📤 Конфигурация», а
    «✏️ Изменить» на том же месте, что у остальных. Открыта
    ссылкой с главной (back_main) — выход «⬅️ В меню»."""
    gw = state["gateway"]
    if back_main:
        exit_row = ui.to_menu(Menu(action="main"))
    else:
        exit_row = ui.back(GwSlotCB(action="list") if back_to_list else SetCB(sec="rt"))
    cfg1 = bool(((state.get("channel") or {}).get("cfg") or {}).get("cfg1"))
    return ui.rows(
        ("▶️ Сделать активным", GwSlotCB(action="switch_ask", slot=gw.id)) if not state.get("active") else None,
        # файл — агентам без документа конфигурации: остальным всё привозит канал
        [("📤 Конфигурация", GwSlotCB(action="bundle", slot=gw.id)) if not cfg1 else None,
         ("📡 Пинг", GwSlotCB(action="ping", slot=gw.id))],
        [(f"{_tick(bool(gw.lan_mode))} VPN-транзит", GwSlotCB(action="lan_ask", slot=gw.id)),
         ("🗺 Подсети", GwSlotCB(action="lan_nets", slot=gw.id))],
        [("🛜 Роутер", GwSlotCB(action="router", slot=gw.id)) if gw.lan_mode else None,
         ("✏️ Изменить", GwSlotCB(action="edit", slot=gw.id))],
        exit_row)


def gateway_edit_kb(state, *, two_slots: bool) -> InlineKeyboardMarkup:
    """«✏️ Изменить» слота: имя устройства и подпись, тумблер «⭐ По
    умолчанию» — по умолчанию при холодном старте (при двух слотах), замена и
    снятие. Ключи линка — не здесь: редкая аварийная кнопка живёт в
    «⚙️ Настройки → 🔧 Сервис»."""
    gw, dev = state["gateway"], state.get("device")
    return ui.rows(
        [("✏️ Имя", GwSlotCB(action="name", slot=gw.id)) if dev is not None else None,
         ("✏️ Подпись", GwSlotCB(action="label", slot=gw.id))],
        (f"{_tick(bool(state.get('preferred')))} ⭐ По умолчанию", GwSlotCB(action="pref", slot=gw.id)) if two_slots else None,
        [("🔁 Заменить", SetCB(sec="rt_gw", act="open", key=str(gw.id))), ("➖ Снять", GwSlotCB(action="remove_ask", slot=gw.id))],
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
    return confirm(cancel, "➖ Снять", GwSlotCB(action="remove_yes", slot=slot))


def gateway_keys_pick(slots) -> InlineKeyboardMarkup:
    """«🔑 Ключи линка» в «🔧 Сервис»: шлюзы, которые умеют смену каналом
    (имя с подписью), «Назад» — в «Сервис»."""
    return ui.rows(*[(name, SetCB(sec="lk", act="do", key="ask", val=str(slot))) for slot, name in slots],
                   ui.back(SetCB(sec="svc")))


def gateway_token_pick(slots) -> InlineKeyboardMarkup:
    """«🤖 Токен бота» в «🔧 Сервис»: шлюзы, которым токен довезёт канал
    (имя с подписью), «Назад» — в «Сервис»."""
    return ui.rows(*[(name, SetCB(sec="gtk", act="do", key="ask", val=str(slot))) for slot, name in slots],
                   ui.back(SetCB(sec="svc")))


def gateway_keys_confirm(slot: int) -> InlineKeyboardMarkup:
    """Смена ключей линка: «Отмена» первой — назад в «🔧 Сервис»."""
    return confirm(SetCB(sec="svc"), "🔑 Сменить", SetCB(sec="lk", act="do", key="yes", val=str(slot)),
                   danger=False)


def gateway_lan_confirm(slot: int, on: bool) -> InlineKeyboardMarkup:
    """Подтверждение «🔀 VPN-транзит»: «Отмена» первой."""
    # цель — в колбэке: «не текущее» на втором нажатии выключало только что включённое
    return confirm(GwSlotCB(action="card", slot=slot), "✅ Включить" if on else "☑️ Выключить",
                   GwSlotCB(action="lan_yes", slot=slot, val="1" if on else "0"), danger=False)


def gateway_smb_confirm(on: bool) -> InlineKeyboardMarkup:
    """Подтверждение «🗂 SMB в туннеле»: «Отмена» первой, назад — в «Шлюзы»."""
    return confirm(GwSlotCB(action="list"), "✅ Включить" if on else "☑️ Выключить",
                   GwSlotCB(action="smb_yes", val="1" if on else "0"), danger=False)


def gateway_router_kb(slot: int, tab: str = "mt") -> InlineKeyboardMarkup:
    """Вкладки рецепта: активная — с «✅»; назад — в карточку."""
    from awgbot.bot.texts.routing import ROUTER_TABS
    return ui.rows(
        [(f"✅ {label}" if key == tab else label, GwSlotCB(action="router", slot=slot, val=key)) for key, label in ROUTER_TABS],
        ui.back(GwSlotCB(action="card", slot=slot)))


def gateway_switch_confirm(slot: int, healthy: bool, from_list: bool = False) -> InlineKeyboardMarkup:
    """Ручное переключение: «Отмена» первой — туда, откуда пришли (список или
    карточка); у лежащего резерва та же подпись, но красная."""
    src = "l" if from_list else ""
    return confirm(GwSlotCB(action="list") if from_list else GwSlotCB(action="card", slot=slot),
                   "▶️ Переключить", GwSlotCB(action="switch_yes", slot=slot, val=src), danger=not healthy)



def routing_disable_confirm() -> InlineKeyboardMarkup:
    """Выключить функцию целиком — с подтверждением; назад — в «Параметры»."""
    return confirm(SetCB(sec="rt_params", act="open"), "☑️ Выключить",
                   SetCB(sec="rt", act="do", key="off!"))


def routing_params_kb(info: dict, lists_every: int) -> InlineKeyboardMarkup:
    """«⚙️ Параметры»: такт, окно, порог и период списков — циклами;
    обновить списки; выключить РФ-доступ."""
    return ui.rows(
        [_cycle("rt_params", "app.routing.probe_seconds", f"⏱ Такт: {info['probe_seconds']} с"),
         _cycle("rt_params", "app.routing.failover.window_samples", f"🪟 Окно: {info['window']}")],
        [_cycle("rt_params", "app.routing.failover.min_availability", f"📉 Порог: {info['availability']}%"),
         _cycle("rt_params", "app.routing.lists_refresh_hours", f"📋 Списки: {lists_every} ч")],
        ("🔄 Обновить списки", SetCB(sec="rt", act="do", key="lists_refresh")),
        ("☑️ Выключить РФ-доступ", SetCB(sec="rt", act="toggle", key="app.routing.enabled")),
        ui.back(SetCB(sec="rt")))


def settings_routing_users(clients=(), marks: dict | None = None, *, dirty: bool = False,
                           page: int = 0) -> InlineKeyboardMarkup:
    """«👥 Кому доступен»: отметки профилей — выбор (marks {id: отмечен}), не
    состояние; «Все профили» по правилу массового выбора; последним рядом
    «✅ Применить» (при отличии от сохранённого) и «⬅️ Назад» в «Шлюзы»."""
    clients = list(clients)
    marks = marks if marks is not None else {c.id: bool(c.routing_allowed) for c in clients}
    return ui.rows(
        *ui.paged(clients, page, static=2 if clients else 1, screen="rtusers", ref=0,
                  back=SetCB(sec="rt_users", act="page").pack(),
                  button=lambda _i, c: (f"{_tick(marks.get(c.id, bool(c.routing_allowed)))} {c.name}",
                                        SetCB(sec="rt", act="do", key="allow", val=str(c.id)))),
        select_all_button(sum(1 for v in marks.values() if v), len(marks),
                          SetCB(sec="rt", act="do", key="allow_all"), "Все профили") if clients else None,
        apply_row(dirty, SetCB(sec="rt", act="do", key="allow_apply"), SetCB(sec="rt")))


def bundle_menu_kb(slot: int = 0, *, plain: bool = False, from_card: bool = False) -> InlineKeyboardMarkup:
    """Под файлом конфигурации: обе кнопки убирают файл и сообщение над ним
    из чата (внутри ключ линка) и открывают карточку слота или главную
    (`bundle_cancel`, `bundle_home`). Выпущен из карточки (from_card) —
    «⬅️ Назад» (в неё) и «⬅️ В меню»; иначе (ссылка из уведомления, файл
    нового шлюза — plain) карточки человек не видел: «🛰 Карточка шлюза» и
    «⬅️ В меню». Переслать файл кнопкой Telegram не даёт — пересылают рукой."""
    return ui.rows([(ui.BACK_LABEL if from_card and not plain else "🛰 Карточка шлюза",
                     SetCB(sec="rt", act="do", key="bundle_cancel", val=str(slot or ""))),
                    (ui.MENU_LABEL, SetCB(sec="rt", act="do", key="bundle_home", val=str(slot or "")))])
