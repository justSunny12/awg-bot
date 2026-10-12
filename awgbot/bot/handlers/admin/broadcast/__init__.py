"""
handlers/admin/broadcast/ — рассылка объявлений, в том числе режим с
продлением подписки.
Части: targets — роутер, кулдаун, выбор адресатов, тумблер продления, дни;
draft — приём черновика (текст, альбом), замки и отложенное превью; send —
отмена и отправка с отчётом. Роутер один (в targets), части регистрируют на нём
и импортируются в порядке прежнего модуля — порядок обработчиков не изменился.
Имена, которые подменяют тесты (call, broadcast, _BC_SETTLE_SECONDS,
_bc_preview), части зовут через пакет.
"""
from __future__ import annotations

from awgbot.bot.handlers.common import call  # noqa: F401 — тесты подменяют через пакет
from awgbot.bot.notifier import broadcast  # noqa: F401 — эталоны экранов подменяют через пакет

from .targets import (
    router, _BROADCAST_COOLDOWN_SEC, _last_broadcast_at, _bc_clients, _bc_selected,
    _bc_show_targets, broadcast_pick, broadcast_mode, broadcast_extend_toggle,
    broadcast_targets_again, broadcast_toggle, broadcast_toggle_all, broadcast_next, _days_chosen,
    broadcast_days_preset, broadcast_days)
from .draft import (
    _BC_SETTLE_SECONDS, _bc_locks, _bc_lock, _bc_render_tasks, _bc_cancel_render, _bc_render_later,
    broadcast_receive, _bc_preview)
from .send import broadcast_cancel_h, broadcast_send

__all__ = ["call", "broadcast", "router", "_BROADCAST_COOLDOWN_SEC", "_last_broadcast_at", "_bc_clients",
    "_bc_selected", "_bc_show_targets", "broadcast_pick", "broadcast_mode",
    "broadcast_extend_toggle", "broadcast_targets_again", "broadcast_toggle",
    "broadcast_toggle_all", "broadcast_next", "_days_chosen", "broadcast_days_preset",
    "broadcast_days", "_BC_SETTLE_SECONDS", "_bc_locks", "_bc_lock", "_bc_render_tasks",
    "_bc_cancel_render", "_bc_render_later", "broadcast_receive", "_bc_preview",
    "broadcast_cancel_h", "broadcast_send"]
