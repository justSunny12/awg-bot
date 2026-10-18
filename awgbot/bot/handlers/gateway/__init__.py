"""
handlers/gateway/ — панель и операции агента шлюза (роль gateway, только админ).
Части: panel — роутер, панель, «🩺 Здоровье», первая панель и панель после
рестарта; sections — ролевые разделы и мостик к общим разделам bot/sections;
dialogs — регистрация общих диалогов (ввод значения, мастер почты);
bundle — файлы в чате (бэкап и файлы конфигурации — отказ словами, сообщение
убирается: внутри ключи); access — «🛡 SSH-доступ»; transit — «🔀 VPN-транзит» со своими
списками и «🛜 Настройкой роутера»; ask — вопросы о документе с сервера AWG: ключи
линка, аплинк, почта, настройки шлюза из бэкапа сервера («⏳ Ответить (N)»,
«⬅️ Оставить своё» / «✅ Принять»).

Общие разделы «⚙️ Настроек» (уведомления, почта, мониторинг, сервис с
перезапусками, обновления; своих бэкапов у шлюза нет) — в bot/sections со словарём GATEWAY; отсюда им —
только экраны ролевых разделов (_section: «ssh», «lan», прочее — панель) через
br.role_screens. Пробы и операции блокирующие (subprocess) — через call, как
всё синхронное в проекте.
"""
from __future__ import annotations

import asyncio   # эталоны экранов подменяют asyncio через пакет (ожидание канала — мгновенно)


from .panel import router, _BUNDLE_MAX_BYTES, _snapshot_max_age, _status, _panel, _panel_parts, restore_panel_after_restart, _FIRST_PANEL_KEY, send_first_panel, gw_start, _refresh_by_link, _bot_username, gw_panel, gw_refresh, gw_health
from .sections import _ssh_section, _SECTIONS, _section, _render, _shared, gw_settings, gw_section, gw_toggle, gw_edit, gw_cycle, gw_email_action, gw_bot_restart, gw_cancel_inline, gw_ssh_section
from .dialogs import _core, gw_receive_value, _hooks
from .bundle import gw_bundle_document
from .access import gw_ssh_port_ask, gw_ssh_port_back, _port_dialog, gw_ssh_port_received, gw_ssh_allow_ask, gw_ssh_allow_received, gw_ssh_action, gw_confirm, gw_execute
from .transit import _own_info, _transit_screen, gw_transit, gw_transit_ask, gw_transit_domain_received, _own_sync_tail, gw_transit_remove, gw_transit_router
from .ask import show_cfg_question, gw_cfg_ask, gw_cfg_answer, _next_or_panel

__all__ = ["show_cfg_question", "gw_cfg_ask", "gw_cfg_answer", "_next_or_panel", "asyncio", "router", "_BUNDLE_MAX_BYTES", "_snapshot_max_age", "_status", "_panel", "_panel_parts", "restore_panel_after_restart", "_FIRST_PANEL_KEY", "send_first_panel", "gw_start", "_refresh_by_link", "_bot_username", "gw_panel", "gw_refresh", "gw_health", "_ssh_section", "_SECTIONS", "_section", "_render", "_shared", "gw_settings", "gw_section", "gw_toggle", "gw_edit", "gw_cycle", "gw_email_action", "gw_bot_restart", "gw_cancel_inline", "gw_ssh_section", "gw_bundle_document", "_core", "gw_receive_value", "_hooks", "gw_ssh_port_ask", "gw_ssh_port_back", "_port_dialog", "gw_ssh_port_received", "gw_ssh_allow_ask", "gw_ssh_allow_received", "gw_ssh_action", "gw_confirm", "gw_execute", "_own_info", "_transit_screen", "gw_transit", "gw_transit_ask", "gw_transit_domain_received", "_own_sync_tail", "gw_transit_remove", "gw_transit_router"]
