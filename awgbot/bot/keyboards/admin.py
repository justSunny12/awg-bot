"""Меню администратора: главная, профили, карточки, продление, лимиты пресетами, устройства без профиля, списки."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from awgbot.core import blocks as _blocks
from awgbot.core import config
from awgbot.core.enums import ActivationStatus, PauseMode
from awgbot.bot import ui
from awgbot.bot.callbacks import (
    AdminSelfCB, BlockCB, CancelCB, ClientCB, DeviceCB, GwSlotCB, Menu, PeriodCB, PresetCB,
    ReassignCB, RoutingCB, SetCB, BroadcastCB)
from awgbot.bot import texts as _texts

from .common import (_btn_suffix, _manual_block_button, confirm, to_menu, MAX_ROWS)


# ─────────────────────────────────────────────────────────────────────────────
# Главная
# ─────────────────────────────────────────────────────────────────────────────

def admin_main(*, routing_visible: bool = False, self_client_id: int = 0) -> InlineKeyboardMarkup:
    """Восемь кнопок: свои устройства и РФ-доступ (когда выдан), профили и
    новый профиль, шлюзы и настройки, объявление и обновление."""
    from .client import menu_issue_row
    # «🛰 Шлюзы» — всегда: это и вход к развёртыванию РФ-доступа, и к его
    # включению, других входов у функции нет
    return ui.rows(
        menu_issue_row(),                     # выдача своим устройствам — первой строкой, как у клиента
        [("📱 Мои устройства", Menu(action="devices")),
         (f"🇷🇺 {_texts.ROUTING_NAME}", RoutingCB(action="panel", ref=self_client_id)) if routing_visible else None],
        [("👥 Профили", Menu(action="clients")), ("➕ Профиль", Menu(action="add_client"))],
        [("🛰 Шлюзы", SetCB(sec="rt")), ("⚙️ Настройки", SetCB(sec="root"))],
        [("📢 Объявление", BroadcastCB(action="pick")), ("🔄 Обновить", Menu(action="refresh"))])


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


def _dev_button(_i, d) -> tuple:
    return (_dev_label(d), DeviceCB(action="open", device_id=d.id))


def admin_devices(devices, page: int = 0, *, can_add: bool = True) -> InlineKeyboardMarkup:
    """«📱 Мои устройства» админа: список (шлюзы вверху), добавить, назад."""
    return ui.rows(
        *ui.paged(_sorted_devices(devices), page, static=1, screen="devices", ref=0,
                  back=Menu(action="devices").pack(), button=_dev_button),
        [("➕ Устройство", AdminSelfCB(action="add")) if can_add else None, ui.back(Menu(action="main"))])


def admin_client_device_list(devices, client_id: int, page: int = 0) -> InlineKeyboardMarkup:
    """Устройства профиля — когда в карточку они не влезли."""
    return ui.rows(
        *ui.paged(_sorted_devices(devices), page, static=1, screen="clidevs", ref=client_id,
                  back=ClientCB(action="devices", client_id=client_id).pack(), button=_dev_button),
        [("➕ Устройство", ClientCB(action="add_device", client_id=client_id)),
         ui.back(ClientCB(action="open", client_id=client_id))])


def unassigned_devices(devices, page: int = 0) -> InlineKeyboardMarkup:
    return ui.rows(
        *ui.paged(devices, page, static=1, screen="unassigned", ref=0, back=Menu(action="unassigned").pack(),
                  button=lambda _i, d: (f"{d.name}{_btn_suffix(d)} · {d.address}", DeviceCB(action="open", device_id=d.id))),
        ui.to_menu(Menu(action="main")))


def reassign_targets(device_id: int, clients, page: int = 0) -> InlineKeyboardMarkup:
    """«🔀 iPhone — в какой профиль?»; «Назад» — в карточку устройства."""
    return ui.rows(
        *ui.paged(clients, page, static=1, screen="reassign", ref=device_id,
                  back=DeviceCB(action="reassign", device_id=device_id).pack(),
                  button=lambda _i, c: (c.name, ReassignCB(device_id=device_id, client_id=c.id, stage="go"))),
        ui.back(DeviceCB(action="open", device_id=device_id)))


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
    return ui.rows(
        *ui.paged(clients, page, static=1, screen="clients", ref=0, back=Menu(action="clients").pack(),
                  button=lambda _i, c: (f"{client_state_icon(c, online)} {c.name}", ClientCB(action="open", client_id=c.id))),
        ui.back(Menu(action="main")))


def admin_client_actions(client, devices=(), *, routing_visible: bool = False) -> InlineKeyboardMarkup:
    """Карточка профиля: условные ряды (снять паузу, новое приглашение), затем
    [⏱ Продлить] [✏️ Изменить] / [🇷🇺 РФ-доступ] [🛑 Блок] / устройства по
    одному, пока влезают в десятку, иначе [📱 Устройства: N] / [➕ Устройство]
    [⬅️ Назад]. У профиля админа карточки нет."""
    devices = _sorted_devices(devices)
    head: list = []
    # пауза блокировкой «до снятия» — не пауза: снимается вместе с блоком
    if (int(client.block_reason) & int(_blocks.ClientBlock.PAUSED)
            and getattr(client, "pause_mode", None) != PauseMode.ADMIN_OPEN):
        head.append(("▶️ Снять паузу", ClientCB(action="resume_pause", client_id=client.id)))
    if client.activation_status == ActivationStatus.PENDING:
        head.append(("🔁 Новое приглашение", ClientCB(action="regen_invite", client_id=client.id)))
    head.append([("⏱ Продлить", ClientCB(action="extend", client_id=client.id)),
                 ("✏️ Изменить", ClientCB(action="edit", client_id=client.id))])
    head.append([(f"🇷🇺 {_texts.ROUTING_NAME}", RoutingCB(action="panel", ref=client.id)) if routing_visible else None,
                 _manual_block_button("cli", client.id, int(client.block_reason), for_admin=True)])
    room = MAX_ROWS - (len(head) + 1)      # ряды: уже собранные плюс «➕ Устройство / ⬅️ Назад»
    if len(devices) <= room:
        body = [_dev_button(0, d) for d in devices]
    else:
        body = [(f"📱 Устройства: {len(devices)}", ClientCB(action="devices", client_id=client.id))]
    return ui.rows(
        *head, *body,
        [("➕ Устройство", ClientCB(action="add_device", client_id=client.id)), ui.back(Menu(action="clients"))])


def client_edit_kb(client_id: int) -> InlineKeyboardMarkup:
    """Подэкран «✏️ Изменить» профиля."""
    return ui.rows(
        [("✏️ Имя", ClientCB(action="edit_name", client_id=client_id)),
         ("✏️ Период", ClientCB(action="edit_period", client_id=client_id))],
        [("✏️ Лимит устр-в", ClientCB(action="edit_limit", client_id=client_id)),
         ("✏️ Трафик", ClientCB(action="edit_traffic", client_id=client_id))],
        ("🗑 Удалить профиль", ClientCB(action="delete", client_id=client_id)),
        ui.back(ClientCB(action="open", client_id=client_id)))


def admin_client_back(client_id: int) -> InlineKeyboardMarkup:
    return ui.rows(ui.back(ClientCB(action="open", client_id=client_id)))


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


def _cancel_button(cancel_cb) -> tuple:
    """«✖️ Отмена» под приглашением (CancelCB реестра экранов), иначе «⬅️ Отмена»."""
    packed = cancel_cb if isinstance(cancel_cb, str) else cancel_cb.pack()
    return ("✖️ Отмена" if packed.startswith(CancelCB.__prefix__ + ":") else "⬅️ Отмена", packed)


def presets_kb(kind: str, ref: int, values, cancel_cb) -> InlineKeyboardMarkup:
    """Пресеты по три в ряд вместе с «✏️ Другое»; отмена — в последний ряд,
    если там есть место, иначе своим рядом. kind — из PresetCB."""
    rows = ui.grid([*[(_preset_label(kind, v), PresetCB(kind=kind, ref=ref, val=v)) for v in values],
                    ("✏️ Другое", PresetCB(kind=kind, ref=ref, val=-1))], 3)
    cancel = _cancel_button(cancel_cb)
    if len(rows[-1]) < 3:
        rows[-1].append(cancel)
    else:
        rows.append([cancel])
    return ui.rows(*rows)


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
    kinds = [k for k in config.PERIOD_CHOICES if k != "never" and _MIN_DAYS.get(k, 0) > min_days]
    cancel = cancel_cb or (ClientCB(action="open", client_id=ref) if ctx == "extend" else CancelCB(kind="main"))
    return ui.rows(
        *ui.grid([(PERIOD_SHORT.get(k, config.PERIOD_LABELS.get(k, k)), PeriodCB(kind=k, ctx=ctx, ref=ref, keep=int(keep)))
                  for k in kinds], 2),
        ("∞", PeriodCB(kind="never", ctx=ctx, ref=ref, keep=0)) if "never" in config.PERIOD_CHOICES else None,
        (("✅" if keep else "☑️") + " Сохранить остаток", PeriodCB(kind="keep_tgl", ctx=ctx, ref=ref, keep=int(not keep)))
        if ctx == "extend" and has_remainder else None,
        _cancel_button(cancel))


# ── блокировка профиля и устройства ─────────────────────────────────────────

def block_pause_kb(client_id: int) -> InlineKeyboardMarkup:
    return ui.rows([("⏸️ Да", BlockCB(target="cli", action="pause_yes", ref=client_id)),
                    ("▶️ Нет", BlockCB(target="cli", action="pause_no", ref=client_id))],
                   ("⬅️ Отмена", BlockCB(target="cli", action="cancel", ref=client_id)))


def block_notify_kb(target: str, ref: int, pause_days: int = -1) -> InlineKeyboardMarkup:
    """Профиль: «Уведомить владельца?» [🔔 Да] [🔕 Нет]; устройство: [🔔 С
    уведомлением] [🔕 Тихо]. pause_days: -1 без паузы, 0 — до снятия."""
    yes, no = ("🔔 Да", "🔕 Нет") if target == "cli" else ("🔔 С уведомлением", "🔕 Тихо")
    return ui.rows([(yes, BlockCB(target=target, action="block", ref=ref, kind="notified", days=pause_days)),
                    (no, BlockCB(target=target, action="block", ref=ref, kind="silent", days=pause_days))],
                   ("⬅️ Отмена", BlockCB(target=target, action="cancel", ref=ref)))


# ── списки главной ───────────────────────────────────────────────────────────

traffic_profiles_kb = to_menu
online_devices_kb = to_menu


def traffic_devices_kb() -> InlineKeyboardMarkup:
    return ui.rows(ui.back(Menu(action="traffic")))


def expiring_kb(rows=()) -> InlineKeyboardMarkup:
    """«⏱ Имя» по два в ряд — продлить прямо из списка; в меню."""
    items = list(rows)[:2 * (MAX_ROWS - 1)]     # по два в ряд, ряд «В меню»
    buttons = []
    for c, _secs in items:
        name = c.name if len(c.name) <= 15 else c.name[:14] + "…"
        buttons.append((f"⏱ {name}", ClientCB(action="extend_exp", client_id=c.id)))
    return ui.rows(*ui.grid(buttons, 2), ui.to_menu(Menu(action="main")))


def invite_menu(plain_text: str, link: str, client_id: int) -> InlineKeyboardMarkup:
    """Под приглашением профиля одним сообщением: «📤 Отправить» и «📋 Скопировать»
    несут только текст приглашения (не сводку под чертой), ниже — «👤 В карточку»
    и «⬅️ На главную»."""
    from .client import invite_kb
    return ui.rows(*invite_kb(plain_text, link).inline_keyboard, *to_client_card(client_id).inline_keyboard)


def to_client_card(client_id: int) -> InlineKeyboardMarkup:
    """«👤 В карточку» и «⬅️ На главную» — завершитель под приглашением профиля."""
    return ui.rows([("👤 В карточку", ClientCB(action="open", client_id=client_id)),
                    ("⬅️ На главную", Menu(action="main"))])


def migration_back_kb() -> InlineKeyboardMarkup:
    """Экран переезда профиля: назад — в обзор переезда."""
    return ui.rows(ui.back(Menu(action="migration")))


def add_device_addslot(client_id: int) -> InlineKeyboardMarkup:
    """Лимит профиля исчерпан при добавлении устройства админом: «⬅️ Отмена»
    — в карточку, «➕ Слот и добавить» — лимит +1 и ввод имени."""
    return confirm(ClientCB(action="open", client_id=client_id), "➕ Слот и добавить",
                   ClientCB(action="add_device_slot", client_id=client_id), danger=False)


def gateway_card_button(slot: int) -> InlineKeyboardMarkup:
    """Под карточкой устройства-шлюза: в карточку слота и назад; слота нет
    (двойник в окне переезда) — только «Назад», кнопка в никуда не нужна."""
    return ui.rows(("🛰 Карточка шлюза", GwSlotCB(action="card", slot=slot)) if slot else None,
                   ui.back(Menu(action="devices")))
