"""
settingscore/ — общая механика экранов настроек для ОБЕИХ ролей.
Части: base — крючки роли (Hooks), приглашение и раздел после ввода; values —
ввод значения с проверками; actions — тумблер, порт SSH, бэкап сейчас, канал
бэкапа, начало ввода парольной фразы; email — действия раздела почты;
register — регистрация обработчиков сообщений.

У основного бота (пакет settings/) и у агента шлюза (gateway.py) одни и те же
диалоги: приём числового/текстового значения, парольная фраза бэкапов в два
шага, «бэкап сейчас», выбор канала бэкапа, пять email-действий, тумблер с
проверкой «ящик настроен». Они жили двумя копиями и расходились: у агента
раздел после ввода приходил через send_menu, у основного — голым answer, и
правка одной копии не доезжала до другой.

Роль отдаёт сюда только то, чем действительно отличается, — крючки (Hooks),
которые собирает из словаря роли sections.hooks_for; сами шаги диалогов здесь.
Обработчики сообщений (состояния одни на обе роли) регистрирует register() на
роутер общих разделов (sections.make_router — по одному на диспетчер); кнопки
общих разделов зовут шаги отсюда через диспетчер bot/sections, ролевые
разделы основного бота (handlers/settings/) — напрямую с hooks_for(MAIN).
"""
from __future__ import annotations

from .base import Hooks, ask, after_input
from .values import (
    start_edit, _validate_server_value, _receive_text, _receive_backup_when, _receive_bonus,
    receive_value)
from .actions import (
    toggle_bool, PortDialog, port_received, backup_now, set_backup_channel, passphrase_start,
    _take_secret_message)
from .email import _render_with_note, email_action
from .register import register

__all__ = ["Hooks", "ask", "after_input", "start_edit", "_validate_server_value", "_receive_text",
    "_receive_backup_when", "_receive_bonus", "receive_value", "toggle_bool", "PortDialog",
    "port_received", "backup_now", "set_backup_channel", "passphrase_start", "_take_secret_message",
    "_render_with_note", "email_action", "register"]
