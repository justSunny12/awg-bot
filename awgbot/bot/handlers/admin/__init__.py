"""
handlers/admin — роутер администратора: пакет роутеров по областям.

Управление клиентами (создание с инвайтом, продление с остатком, редактирование,
удаление), выдача конфигов, статус сервера, бэкап, перезапуск сервиса, работа с
устройствами без клиента (привязка, удаление).

Снаружи пакет виден как один роутер `router` (имя «admin», фильтры роли на
сообщениях и колбэках), внутрь включены подроутеры:

  panel      панель, /start с deep-link'ами, истекающие, потребление, обновить статус
  clients    профили: список, карточка, создание, имя/лимиты/период/продление/удаление
  devices    устройства: добавление профилю, выдача, бесхозные, карточка, перепривязка
  gateway    пометка шлюза по пересланному сообщению агента
  updates    self-update
  selfops    личные устройства админа (AdminSelfCB)
  blocks     ручные блокировки устройств и клиентов
  broadcast  объявления, в том числе с продлением

Порядок включения — порядок проверки фильтров. panel идёт ПЕРВЫМ: его
`admin_start` (CommandStart без ограничения по состоянию) обязан перехватывать
«/start» раньше любого FSM-обработчика ввода из остальных модулей и раньше
`gateway_claim_message`; сторож — tests/smoke/test_admin_router_order.py.
"""

from aiogram import Router

from awgbot.bot.filters import RoleFilter
from awgbot.bot.handlers.admin import (blocks, broadcast, clients, devices, gateway, panel,
                                       selfops, updates)
from awgbot.bot.handlers.admin.panel import (
    _panel_parts, restore_panel_after_restart,
    admin_start, admin_document, admin_expiring, admin_traffic_profiles, admin_main_menu,
    refresh_status)
from awgbot.bot.handlers.admin.clients import (
    clients_list, client_open, add_client_start, add_client_name, add_client_limit,
    add_client_traffic, add_client_period, edit_name_start, edit_name_apply, edit_limit_start,
    edit_limit_apply, edit_limit_confirm, edit_client_traffic_start, edit_device_traffic_start,
    edit_traffic_apply, regen_invite, client_delete_confirm, client_delete_apply, extend_start,
    extend_period_chosen, extend_keep_answer, edit_period_start, edit_period_start_apply,
    edit_period_end_apply, admin_resume_pause)
from awgbot.bot.handlers.admin.devices import (
    admin_add_device_start, admin_add_device_name, admin_add_device_traffic, admin_gen_for,
    admin_client_devices, admin_dev_gen, unassigned_list, admin_device_open,
    admin_device_connect_menu, device_reassign_start, device_reassign_apply,
    device_reassign_slot_yes, device_reassign_slot_no, device_edit_name_start,
    device_edit_name_apply, admin_add_device_choice, admin_add_device_pick, admin_del_ask,
    admin_del_confirm)
from awgbot.bot.handlers.admin.gateway import has_gw_token, gateway_claim_message
from awgbot.bot.handlers.admin.updates import update_install, update_menu, update_mute
from awgbot.bot.handlers.admin.selfops import (
    self_devices, self_gen_pick, self_add_start, self_add_name, self_add_traffic,
    admin_menu_devices)
from awgbot.bot.handlers.admin.blocks import (
    admin_block_menu, admin_block_pause_no, admin_block_pause_yes, admin_block_pause_days,
    admin_unblock_menu, admin_block_do, admin_unblock_do, admin_block_cancel)
from awgbot.bot.handlers.admin.broadcast import (
    broadcast_pick, broadcast_mode, broadcast_toggle, broadcast_toggle_all, broadcast_next,
    broadcast_days, broadcast_receive, broadcast_cancel_h, broadcast_send)

router = Router(name="admin")
router.message.filter(RoleFilter("admin"))
router.callback_query.filter(RoleFilter("admin"))

# panel — первым (см. докстринг модуля); остальные между собой по фильтрам не
# пересекаются, порядок — как шли секции в прежнем admin.py.
router.include_router(panel.router)
router.include_router(clients.router)
router.include_router(devices.router)
router.include_router(gateway.router)
router.include_router(updates.router)
router.include_router(selfops.router)
router.include_router(blocks.router)
router.include_router(broadcast.router)

__all__ = [
    "router",
    # помощники панели: runtime/main.py (после рестарта) и handlers/common.py
    "_panel_parts", "restore_panel_after_restart",
    # panel
    "admin_start", "admin_document", "admin_expiring", "admin_traffic_profiles",
    "admin_main_menu", "refresh_status",
    # clients
    "clients_list", "client_open", "add_client_start", "add_client_name", "add_client_limit",
    "add_client_traffic", "add_client_period", "edit_name_start", "edit_name_apply",
    "edit_limit_start", "edit_limit_apply", "edit_limit_confirm", "edit_client_traffic_start",
    "edit_device_traffic_start", "edit_traffic_apply", "regen_invite", "client_delete_confirm",
    "client_delete_apply", "extend_start", "extend_period_chosen", "extend_keep_answer",
    "edit_period_start", "edit_period_start_apply", "edit_period_end_apply",
    "admin_resume_pause",
    # devices
    "admin_add_device_start", "admin_add_device_name", "admin_add_device_traffic",
    "admin_gen_for", "admin_client_devices", "admin_dev_gen", "unassigned_list",
    "admin_device_open", "admin_device_connect_menu", "device_reassign_start",
    "device_reassign_apply", "device_reassign_slot_yes", "device_reassign_slot_no",
    "device_edit_name_start", "device_edit_name_apply", "admin_add_device_choice",
    "admin_add_device_pick", "admin_del_ask", "admin_del_confirm",
    # gateway
    "has_gw_token", "gateway_claim_message",
    # updates
    "update_install", "update_menu", "update_mute",
    # selfops
    "self_devices", "self_gen_pick", "self_add_start", "self_add_name", "self_add_traffic",
    "admin_menu_devices",
    # blocks
    "admin_block_menu", "admin_block_pause_no", "admin_block_pause_yes",
    "admin_block_pause_days", "admin_unblock_menu", "admin_block_do", "admin_unblock_do",
    "admin_block_cancel",
    # broadcast (модульное состояние рассылки — _BC_SETTLE_SECONDS, _bc_preview,
    # _bc_render_tasks, _last_broadcast_at — НАРОЧНО не реэкспортируется: подмена
    # атрибута пакета не меняет глобал подмодуля; тесты патчат admin.broadcast)
    "broadcast_pick", "broadcast_mode", "broadcast_toggle", "broadcast_toggle_all",
    "broadcast_next", "broadcast_days", "broadcast_receive", "broadcast_cancel_h",
    "broadcast_send",
]
