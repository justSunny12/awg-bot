"""Меню администратора: главная, профили, карточки, продление, лимиты пресетами, устройства без профиля, списки."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from awgbot.core import blocks as _blocks
from awgbot.core import config
from awgbot.core.enums import ActivationStatus, PauseMode
from awgbot.bot.callbacks import (
    AdminSelfCB, BlockCB, CancelCB, ClientCB, DeviceCB, GwSlotCB, Menu, PeriodCB, PresetCB,
    ReassignCB, RoutingCB, SetCB, BroadcastCB)
from awgbot.bot import texts as _texts

from .common import (paged_rows, _btn_suffix, _manual_block_button, confirm, to_menu,
                     MAX_ROWS)


# ─────────────────────────────────────────────────────────────────────────────
# Главная
# ─────────────────────────────────────────────────────────────────────────────

def admin_main(*, gateways: bool = False, routing_visible: bool = False,
               self_client_id: int = 0) -> InlineKeyboardMarkup:
    """Восемь кнопок: свои устройства и РФ-доступ (когда выдан), профили и
    новый профиль, шлюзы и настройки, объявление и обновление."""
    kb = InlineKeyboardBuilder()
    rows = []
    kb.button(text="📱 Мои устройства", callback_data=Menu(action="devices"))
    if routing_visible:
        kb.button(text=f"🇷🇺 {_texts.ROUTING_NAME}", callback_data=RoutingCB(action="panel", ref=self_client_id))
        rows.append(2)
    else:
        rows.append(1)
    kb.button(text="👥 Профили", callback_data=Menu(action="clients"))
    kb.button(text="➕ Профиль", callback_data=Menu(action="add_client"))
    rows.append(2)
    # «🛰 Шлюзы» — всегда: это и вход к развёртыванию РФ-доступа, и к его
    # включению, других входов у функции нет
    kb.button(text="🛰 Шлюзы", callback_data=SetCB(sec="rt"))
    kb.button(text="⚙️ Настройки", callback_data=SetCB(sec="root"))
    rows.append(2)
    kb.button(text="📢 Объявление", callback_data=BroadcastCB(action="pick"))
    kb.button(text="🔄 Обновить", callback_data=Menu(action="refresh"))
    rows.append(2)
    kb.adjust(*rows)
    return kb.as_markup()


# ─────────────────────────────────────────────────────────────────────────────
# Мои устройства, устройства профиля, без профиля
# ─────────────────────────────────────────────────────────────────────────────

def _dev_label(d) -> str:
    if getattr(d, "is_gateway", 0):
        return f"🛰 {d.name} [шлюз]"
    lent = f" [{_texts.holder_name(d)}]" if getattr(d, "is_lent", False) else ""
    return f"{_texts.device_state(d, for_admin=True)} {d.name}{_btn_suffix(d)}{lent}"


def _sorted_devices(devices) -> list:
    """Шлюзы — всегда вверху."""
    return sorted(devices, key=lambda d: 0 if getattr(d, "is_gateway", 0) else 1)


def admin_devices(devices, page: int = 0, *, can_add: bool = True) -> InlineKeyboardMarkup:
    """«📱 Мои устройства» админа: список (шлюзы вверху), добавить, назад."""
    kb = InlineKeyboardBuilder()
    rows = paged_rows(kb, _sorted_devices(devices), page, static=1, screen="devices", ref=0,
                      back=Menu(action="devices").pack(),
                      button=lambda _i, d: kb.button(text=_dev_label(d), callback_data=DeviceCB(action="open", device_id=d.id)))
    tail = 0
    if can_add:
        kb.button(text="➕ Устройство", callback_data=AdminSelfCB(action="add"))
        tail += 1
    kb.button(text="⬅️ Назад", callback_data=Menu(action="main"))
    tail += 1
    kb.adjust(*rows, tail)
    return kb.as_markup()


def admin_client_device_list(devices, client_id: int, page: int = 0) -> InlineKeyboardMarkup:
    """Устройства профиля — когда в карточку они не влезли."""
    kb = InlineKeyboardBuilder()
    rows = paged_rows(kb, _sorted_devices(devices), page, static=1, screen="clidevs", ref=client_id,
                      back=ClientCB(action="devices", client_id=client_id).pack(),
                      button=lambda _i, d: kb.button(text=_dev_label(d), callback_data=DeviceCB(action="open", device_id=d.id)))
    kb.button(text="➕ Устройство", callback_data=ClientCB(action="add_device", client_id=client_id))
    kb.button(text="⬅️ Назад", callback_data=ClientCB(action="open", client_id=client_id))
    kb.adjust(*rows, 2)
    return kb.as_markup()


def unassigned_devices(devices, page: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    rows = paged_rows(kb, devices, page, static=1, screen="unassigned", ref=0, back=Menu(action="unassigned").pack(),
                      button=lambda _i, d: kb.button(text=f"{d.name}{_btn_suffix(d)} · {d.address}",
                                                     callback_data=DeviceCB(action="open", device_id=d.id)))
    kb.button(text="⬅️ В меню", callback_data=Menu(action="main"))
    kb.adjust(*rows, 1)
    return kb.as_markup()


def reassign_targets(device_id: int, clients, page: int = 0) -> InlineKeyboardMarkup:
    """«🔀 iPhone — в какой профиль?»; «Назад» — в карточку устройства."""
    kb = InlineKeyboardBuilder()
    rows = paged_rows(kb, clients, page, static=1, screen="reassign", ref=device_id,
                      back=DeviceCB(action="reassign", device_id=device_id).pack(),
                      button=lambda _i, c: kb.button(text=c.name, callback_data=ReassignCB(device_id=device_id, client_id=c.id, stage="go")))
    kb.button(text="⬅️ Назад", callback_data=DeviceCB(action="open", device_id=device_id))
    kb.adjust(*rows, 1)
    return kb.as_markup()


def reassign_addslot(device_id: int, client_id: int) -> InlineKeyboardMarkup:
    return confirm(DeviceCB(action="open", device_id=device_id), "➕ Слот и перенести",
                   ReassignCB(device_id=device_id, client_id=client_id, stage="slot_yes"), danger=False)


# ─────────────────────────────────────────────────────────────────────────────
# Профили
# ─────────────────────────────────────────────────────────────────────────────

def client_state_icon(c, online_ids) -> str:
    """Один значок по приоритету: ⛔ блок → ⏸️ пауза → ⏳ не активирован →
    🟢 онлайн → ⚪ нет."""
    mask = int(c.block_reason)
    paused = int(_blocks.ClientBlock.PAUSED)
    if mask & ~paused:
        return "⛔"
    if mask & paused:
        return "⏸️"
    if c.activation_status == ActivationStatus.PENDING:
        return "⏳"
    return "🟢" if c.id in online_ids else "⚪"


def admin_clients(clients, online_ids=(), page: int = 0) -> InlineKeyboardMarkup:
    online = set(online_ids or ())
    kb = InlineKeyboardBuilder()
    rows = paged_rows(kb, clients, page, static=1, screen="clients", ref=0, back=Menu(action="clients").pack(),
                      button=lambda _i, c: kb.button(text=f"{client_state_icon(c, online)} {c.name}",
                                                     callback_data=ClientCB(action="open", client_id=c.id)))
    kb.button(text="⬅️ Назад", callback_data=Menu(action="main"))
    kb.adjust(*rows, 1)
    return kb.as_markup()


def admin_client_actions(client, devices=(), *, routing_visible: bool = False) -> InlineKeyboardMarkup:
    """Карточка профиля: условные ряды (снять паузу, новое приглашение), затем
    [⏱ Продлить] [✏️ Изменить] / [🇷🇺 РФ-доступ] [🛑 Блок] / устройства по
    одному, пока влезают в десятку, иначе [📱 Устройства: N] / [➕ Устройство]
    [⬅️ Назад]. У профиля админа карточки нет."""
    kb = InlineKeyboardBuilder()
    rows = []
    devices = _sorted_devices(devices)
    # пауза блокировкой «до снятия» — не пауза: снимается вместе с блоком
    if (int(client.block_reason) & int(_blocks.ClientBlock.PAUSED)
            and getattr(client, "pause_mode", None) != PauseMode.ADMIN_OPEN):
        kb.button(text="▶️ Снять паузу", callback_data=ClientCB(action="resume_pause", client_id=client.id))
        rows.append(1)
    if client.activation_status == ActivationStatus.PENDING:
        kb.button(text="🔁 Новое приглашение", callback_data=ClientCB(action="regen_invite", client_id=client.id))
        rows.append(1)
    kb.button(text="⏱ Продлить", callback_data=ClientCB(action="extend", client_id=client.id))
    kb.button(text="✏️ Изменить", callback_data=ClientCB(action="edit", client_id=client.id))
    rows.append(2)
    bt, bcb = _manual_block_button("cli", client.id, int(client.block_reason), for_admin=True)
    if routing_visible:
        kb.button(text=f"🇷🇺 {_texts.ROUTING_NAME}", callback_data=RoutingCB(action="panel", ref=client.id))
        kb.button(text=bt, callback_data=bcb)
        rows.append(2)
    else:
        kb.button(text=bt, callback_data=bcb)
        rows.append(1)
    fixed = len(rows) + 1                  # ряды: уже собранные плюс «➕ Устройство / ⬅️ Назад»
    room = MAX_ROWS - fixed
    if len(devices) <= room:
        for d in devices:
            kb.button(text=_dev_label(d), callback_data=DeviceCB(action="open", device_id=d.id))
            rows.append(1)
    elif devices:
        kb.button(text=f"📱 Устройства: {len(devices)}", callback_data=ClientCB(action="devices", client_id=client.id))
        rows.append(1)
    kb.button(text="➕ Устройство", callback_data=ClientCB(action="add_device", client_id=client.id))
    kb.button(text="⬅️ Назад", callback_data=Menu(action="clients"))
    rows.append(2)
    kb.adjust(*rows)
    return kb.as_markup()


def client_edit_kb(client_id: int) -> InlineKeyboardMarkup:
    """Подэкран «✏️ Изменить» профиля."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Имя", callback_data=ClientCB(action="edit_name", client_id=client_id))
    kb.button(text="✏️ Период", callback_data=ClientCB(action="edit_period", client_id=client_id))
    kb.button(text="✏️ Лимит устр-в", callback_data=ClientCB(action="edit_limit", client_id=client_id))
    kb.button(text="✏️ Трафик", callback_data=ClientCB(action="edit_traffic", client_id=client_id))
    kb.button(text="🗑 Удалить профиль", callback_data=ClientCB(action="delete", client_id=client_id))
    kb.button(text="⬅️ Назад", callback_data=ClientCB(action="open", client_id=client_id))
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def admin_client_back(client_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=ClientCB(action="open", client_id=client_id))
    return kb.as_markup()


def client_delete_confirm(client_id: int) -> InlineKeyboardMarkup:
    return confirm(ClientCB(action="edit", client_id=client_id), "🗑 Удалить",
                   ClientCB(action="delete_yes", client_id=client_id))


# ── пресеты чисел ────────────────────────────────────────────────────────────

DEVS_PRESETS = (1, 2, 3, 5, 10, 0)             # 0 — ∞
TRAFFIC_PRESETS = (50, 100, 200, 500, 0)       # ГБ, 0 — ∞


def _preset_label(kind: str, v: int) -> str:
    if v == 0:
        return "∞"
    return f"{v} ГБ" if kind.endswith("traffic") else str(v)


def presets_kb(kind: str, ref: int, values, cancel_cb) -> InlineKeyboardMarkup:
    """Пресеты по три в ряд, «✏️ Другое» и отмена. kind — из PresetCB."""
    kb = InlineKeyboardBuilder()
    vals = list(values)
    for v in vals:
        kb.button(text=_preset_label(kind, v), callback_data=PresetCB(kind=kind, ref=ref, val=v))
    kb.button(text="✏️ Другое", callback_data=PresetCB(kind=kind, ref=ref, val=-1))
    cancel = cancel_cb if isinstance(cancel_cb, str) else cancel_cb.pack()
    label = "✖️ Отмена" if cancel.startswith(CancelCB.__prefix__ + ":") else "⬅️ Отмена"
    kb.button(text=label, callback_data=cancel)
    # по три в ряд вместе с «Другое»; отмена — в последний ряд, если там есть
    # место, иначе своим рядом
    n = len(vals) + 1
    rows = [3] * (n // 3) + ([n % 3] if n % 3 else [])
    if rows[-1] < 3:
        rows[-1] += 1
    else:
        rows.append(1)
    kb.adjust(*rows)
    return kb.as_markup()


def devs_limit_kb(client_id: int) -> InlineKeyboardMarkup:
    return presets_kb("cli_devs", client_id, DEVS_PRESETS, ClientCB(action="edit", client_id=client_id))


def traffic_limit_kb(client_id: int) -> InlineKeyboardMarkup:
    return presets_kb("cli_traffic", client_id, TRAFFIC_PRESETS, ClientCB(action="edit", client_id=client_id))


def new_profile_devs_kb() -> InlineKeyboardMarkup:
    return presets_kb("new_devs", 0, DEVS_PRESETS, CancelCB(kind="main"))


def new_profile_traffic_kb() -> InlineKeyboardMarkup:
    return presets_kb("new_traffic", 0, TRAFFIC_PRESETS, CancelCB(kind="main"))


PERIOD_SHORT = {"day": "День", "week": "Неделя", "month": "Месяц", "year": "Год"}


def period_kb(ctx: str, ref: int = 0, *, min_days: int = 0, keep: bool = True,
              has_remainder: bool = False, cancel_cb=None) -> InlineKeyboardMarkup:
    """Сроки по два в ряд, «∞», тумблер «сохранить остаток» (только при остатке)
    и отмена. Пресеты короче отсрочки не показываем."""
    _MIN_DAYS = {"day": 1, "week": 7, "month": 28, "year": 365}
    kb = InlineKeyboardBuilder()
    rows = []
    kinds = [k for k in config.PERIOD_CHOICES if k != "never" and _MIN_DAYS.get(k, 0) > min_days]
    for k in kinds:
        kb.button(text=PERIOD_SHORT.get(k, config.PERIOD_LABELS.get(k, k)),
                  callback_data=PeriodCB(kind=k, ctx=ctx, ref=ref, keep=int(keep)))
    n = len(kinds)
    rows += [2] * (n // 2) + ([1] if n % 2 else [])
    if "never" in config.PERIOD_CHOICES:
        kb.button(text="∞",
                  callback_data=PeriodCB(kind="never", ctx=ctx, ref=ref, keep=0))
        rows.append(1)
    if ctx == "extend" and has_remainder:
        kb.button(text=("✅" if keep else "☑️") + " Сохранить остаток",
                  callback_data=PeriodCB(kind="keep_tgl", ctx=ctx, ref=ref, keep=int(not keep)))
        rows.append(1)
    cancel = cancel_cb or (ClientCB(action="open", client_id=ref) if ctx == "extend" else CancelCB(kind="main"))
    packed = cancel if isinstance(cancel, str) else cancel.pack()
    label = "✖️ Отмена" if packed.startswith(CancelCB.__prefix__ + ":") else "⬅️ Отмена"
    kb.button(text=label, callback_data=packed)
    rows.append(1)
    kb.adjust(*rows)
    return kb.as_markup()


# ── блокировка профиля и устройства ─────────────────────────────────────────

def block_pause_kb(client_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⏸️ Да", callback_data=BlockCB(target="cli", action="pause_yes", ref=client_id))
    kb.button(text="▶️ Нет", callback_data=BlockCB(target="cli", action="pause_no", ref=client_id))
    kb.button(text="⬅️ Отмена", callback_data=BlockCB(target="cli", action="cancel", ref=client_id))
    kb.adjust(2, 1)
    return kb.as_markup()


def block_notify_kb(target: str, ref: int, pause_days: int = -1) -> InlineKeyboardMarkup:
    """Профиль: «Уведомить владельца?» [🔔 Да] [🔕 Нет]; устройство: [🔔 С
    уведомлением] [🔕 Тихо]. pause_days: -1 без паузы, 0 — до снятия."""
    kb = InlineKeyboardBuilder()
    yes, no = ("🔔 Да", "🔕 Нет") if target == "cli" else ("🔔 С уведомлением", "🔕 Тихо")
    kb.button(text=yes, callback_data=BlockCB(target=target, action="block", ref=ref, kind="notified", days=pause_days))
    kb.button(text=no, callback_data=BlockCB(target=target, action="block", ref=ref, kind="silent", days=pause_days))
    kb.button(text="⬅️ Отмена", callback_data=BlockCB(target=target, action="cancel", ref=ref))
    kb.adjust(2, 1)
    return kb.as_markup()


# ── списки главной ───────────────────────────────────────────────────────────

traffic_profiles_kb = to_menu
online_devices_kb = to_menu


def traffic_devices_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=Menu(action="traffic"))
    return kb.as_markup()


def expiring_kb(rows=()) -> InlineKeyboardMarkup:
    """«⏱ Имя» по два в ряд — продлить прямо из списка; в меню."""
    kb = InlineKeyboardBuilder()
    items = list(rows)[:2 * (MAX_ROWS - 1)]     # по два в ряд, ряд «В меню»
    for c, _secs in items:
        name = c.name if len(c.name) <= 15 else c.name[:14] + "…"
        kb.button(text=f"⏱ {name}", callback_data=ClientCB(action="extend_exp", client_id=c.id))
    n = len(items)
    kb.button(text="⬅️ В меню", callback_data=Menu(action="main"))
    kb.adjust(*([2] * (n // 2) + ([1] if n % 2 else [])), 1)
    return kb.as_markup()


def to_client_card(client_id: int) -> InlineKeyboardMarkup:
    """«👤 В карточку» и «⬅️ На главную» — завершитель под приглашением профиля."""
    b = InlineKeyboardBuilder()
    b.button(text="👤 В карточку", callback_data=ClientCB(action="open", client_id=client_id))
    b.button(text="⬅️ На главную", callback_data=Menu(action="main"))
    b.adjust(2)
    return b.as_markup()


def migration_back_kb() -> InlineKeyboardMarkup:
    """Экран переезда профиля: назад — в обзор переезда."""
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=Menu(action="migration"))
    return kb.as_markup()


def add_device_addslot(client_id: int) -> InlineKeyboardMarkup:
    """Лимит профиля исчерпан при добавлении устройства админом: «⬅️ Отмена»
    — в карточку, «➕ Слот и добавить» — лимит +1 и ввод имени."""
    return confirm(ClientCB(action="open", client_id=client_id), "➕ Слот и добавить",
                   ClientCB(action="add_device_slot", client_id=client_id), danger=False)


def gateway_card_button(slot: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛰 Карточка шлюза", callback_data=GwSlotCB(action="card", slot=slot))
    kb.button(text="⬅️ Назад", callback_data=Menu(action="devices"))
    kb.adjust(1, 1)
    return kb.as_markup()
