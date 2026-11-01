"""section — раздел «🛟 Резерв» без пары (Т-Р1) и карточка резерва (Т-Р9),
диалоги удаления и выключения готовности, экран «🆘 Аварийный код».

Части: root — раздел
без пары, строка DNS-записи, «▸ подробнее», всплывашка готовности; card —
карточка резерва; votes — блок голосов кворума карточки; rf — РФ-доступ через
второй сервер (строки карточки и диалог выключения); dialogs — удаление пары,
выключение готовности, аварийный код; move — переезд и возврат кнопкой, возврат
без резерва; dnsrec — экран «🌐 DNS-запись» и приглашения токена.

Имена берутся по-прежнему из пакета (section.card_text, from .section import _n); имя
сервера «…», время и дата — из общего ../common (его берут и соседи section)."""
from __future__ import annotations

from ..common import _n, _when, _date
from .root import ready_on_toast, ABOUT, MORE, more_text, _checked, dns_line, section_text
from .card import LOW_MEM_MB, _KEEP_LINES, _move_step, card_text
from .votes import _W, _qn, _quorum_off_why, quorum_mine_lines, quorum_peer_lines, _cand_tail
from .dialogs import unpair_dialog, ready_off_dialog, emergency_text, new_code_dialog
from .move import move_dialog, move_live, return_dialog, force_dialog, _force_quorum_line, evict_dialog
from .dnsrec import DNS_MORE, dns_text, dns_token_prompt
from .rf import rfpeer_mine_lines, rfpeer_peer_lines, rfmine_off_dialog

__all__ = ["_n", "_when", "_date", "ready_on_toast", "ABOUT", "MORE", "more_text", "_checked", "dns_line",
           "section_text", "LOW_MEM_MB", "_KEEP_LINES", "_move_step", "card_text", "_W", "_qn", "_quorum_off_why",
           "quorum_mine_lines", "quorum_peer_lines", "_cand_tail", "unpair_dialog", "ready_off_dialog",
           "emergency_text", "new_code_dialog", "move_dialog", "move_live", "return_dialog", "force_dialog",
           "_force_quorum_line", "evict_dialog", "DNS_MORE", "dns_text", "dns_token_prompt", "rfpeer_mine_lines",
           "rfpeer_peer_lines", "rfmine_off_dialog"]
