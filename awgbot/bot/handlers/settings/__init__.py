"""handlers/settings/ — ролевые разделы настроек основного бота (только админ):
«🖥 Сервер AWG», «🛡 SSH-доступ», «💳 Подписки», свой резолвер, переезд
профилей, «🛰 Шлюзы» и слоты. Корень «⚙️ Настроек» и общие с агентом разделы
(уведомления, почта, мониторинг, бэкапы, сервис, обновления) — в bot/sections;
их роутер включён раньше этого, поэтому широкие фильтры здесь (act == "do",
"edit", "cycle") их колбэков не видят.

Значения хранятся в conf/*.yaml и меняются через settings.set_value → горячо,
без рестарта. Экран перерисовывается после каждого изменения и показывает
актуальные значения. Раздел под RoleFilter("admin"), как остальная админка.

Части пакета (роутер один на все — _router; порядок регистрации обработчиков
задаёт порядок импорта ниже):

  render    экраны ролевых разделов (_screen — br.role_screens основного бота),
            сообщения о файле конфигурации шлюза
  gwmark    назначение шлюза: выбор устройства, новое устройство, токен агента
  slots     карточки и действия слотов шлюзов
  sections  открытие разделов, тумблеры, действия РФ-доступа
  inputs    свой DNS-резолвер, порты, ввод значений
  cycles    кнопки-циклы и выбор из ряда
  actions   переезд профилей, восстановление, шифрование, почта, общий do

Прежние имена модуля реэкспортируются отсюда.
"""

from awgbot.core import settings  # noqa: F401 — тесты и соседи читают через пакет
from ._router import router  # noqa: F401
from .render import (_screen, gateways_screen, _render, _render_nav, _shared, _record, send_gw_bundle, _bundle_chat, _bundle_display, _drop_bundle_msgs, bundle_applied, _show_card_anew, bundle_installed, card_kb)  # noqa: F401
from .gwmark import (_slot_of, gateway_pick_list, gateway_pick, gateway_mark_yes, gateway_new_ask, gateway_new_yes, gateway_token_received, _gateway_setup_go, _send_plain_bundle)  # noqa: F401
from .slots import (_slot_state, _render_card, _render_list, gw_slot_list, gw_slot_card, gw_slot_add, gw_slot_failover, gw_slot_peer_ask, gw_slot_peer_yes, gw_slot_edit, gw_slot_pref, gw_slot_ping, gw_slot_switch_ask, gw_slot_switch_yes, gw_slot_lan_ask, gw_slot_lan_yes, gw_slot_router, gw_slot_bundle, _issue_bundle_here, gw_slot_home, gateway_home_received, gateway_edit_screen, gw_slot_name, gw_slot_label, gateway_label_received, _remove_ask, gw_slot_remove_ask, gateway_remove_ask, gw_slot_remove_yes, gateway_remove_yes)  # noqa: F401
from .sections import (open_section, toggle, routing_action)  # noqa: F401
from .inputs import (private_dns_action, migration_port_ask, ssh_port_ask, _port_dialog, ssh_port_received, ssh_port_finisher_action, edit_value, _migration_prepare, migration_port_received, _routing_provision, _firewall_action)  # noqa: F401
from .cycles import (_RT_MON_PICKS, _CYCLES, _next_in_cycle, cycle, pick)  # noqa: F401
from .actions import (migration_action, backup_restore_action, backup_passphrase_start, email_action, _core, receive_value, backup_passphrase_first, backup_passphrase_second, email_address, email_imap_host, email_imap_port, email_smtp_host, email_smtp_port, email_password, do_action)  # noqa: F401

__all__ = ["router", "settings", "_screen", "gateways_screen", "_render", "_render_nav", "_shared", "_record", "send_gw_bundle", "_bundle_chat", "_bundle_display", "_drop_bundle_msgs", "bundle_applied", "_show_card_anew", "bundle_installed", "card_kb", "_slot_of", "gateway_pick_list", "gateway_pick", "gateway_mark_yes", "gateway_new_ask", "gateway_new_yes", "gateway_token_received", "_gateway_setup_go", "_send_plain_bundle", "_slot_state", "_render_card", "_render_list", "gw_slot_list", "gw_slot_card", "gw_slot_add", "gw_slot_failover", "gw_slot_peer_ask", "gw_slot_peer_yes", "gw_slot_edit", "gw_slot_pref", "gw_slot_ping", "gw_slot_switch_ask", "gw_slot_switch_yes", "gw_slot_lan_ask", "gw_slot_lan_yes", "gw_slot_router", "gw_slot_bundle", "_issue_bundle_here", "gw_slot_home", "gateway_home_received", "gateway_edit_screen", "gw_slot_name", "gw_slot_label", "gateway_label_received", "_remove_ask", "gw_slot_remove_ask", "gateway_remove_ask", "gw_slot_remove_yes", "gateway_remove_yes", "open_section", "toggle", "routing_action", "private_dns_action", "migration_port_ask", "ssh_port_ask", "_port_dialog", "ssh_port_received", "ssh_port_finisher_action", "edit_value", "_migration_prepare", "migration_port_received", "_routing_provision", "_firewall_action", "_RT_MON_PICKS", "_CYCLES", "_next_in_cycle", "cycle", "pick", "migration_action", "backup_restore_action", "backup_passphrase_start", "email_action", "_core", "receive_value", "backup_passphrase_first", "backup_passphrase_second", "email_address", "email_imap_host", "email_imap_port", "email_smtp_host", "email_smtp_port", "email_password", "do_action"]
