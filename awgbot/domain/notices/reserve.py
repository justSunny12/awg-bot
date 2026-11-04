"""reserve.py — часть пакета notices (см. __init__): уведомления пары серверов
из доменного слоя — связь с резервом (Т-РУ8), совместимость направлений
(Т-РУ9), смена админа второго сервера, удаление пары второй стороной — здесь;
остальное — соседями, а этот модуль отдаёт их имена под прежним путём
(from awgbot.domain.notices import reserve as rn; rn.hosting_started и т. п.):
reserve_move — переезд и возврат пользователей (админу резерва, админу основы,
итоги возврата, запись DNS, пользователям «переподключи VPN», возврат без
резерва); reserve_gw — шлюзы и резерв, агент шлюза; reserve_quorum — кворум
и аварийный код; reserve_tg — пул адресов Telegram и вебхук; reserve_rf —
РФ-доступ через второй сервер; reserve_fmt — общие помощники. Журнал данных
(хвост старой эпохи, Т-Ж4…Т-Ж6, строки Т-РУ5б о недошедших правках) —
reserve_tail. Имена серверов — «имя», адреса и имена записей моноширинным, без
точки в конце; «один раз на расхождение»."""
from __future__ import annotations

from .reserve_fmt import _e, _n, _when
from .reserve_move import (hosting_started, hosting_ended, hosting_failed, hosting_without_bot, return_resumed,
                           moved_to_reserve, returned_home, lost_device, lost_orphan, primary_back, move_failed,
                           return_failed, record_not_changed, record_own_unknown, record_manual_needed, record_missing,
                           record_multi, record_foreign, dns_token_rotate, client_moved, client_returned, forced_home,
                           take_tg_stuck, returned_without_replica, forced_no_merge, webhook_stuck_removed)
from .reserve_gw import (gateway_reserve_down, gateways_reserve_all_down, gateway_reserve_up, gateway_moved,
                         gateway_tg_via, gateway_move_failed)
from .reserve_quorum import (quorum_code_needed_reserve, quorum_code_needed_primary, code_window_closed, code_wrong,
                             code_open_failed, _names, quorum_enabled, quorum_fenced, quorum_unfenced,
                             quorum_enabled_peer, _qn, _qnames, _tie_head, _tie_info, quorum_tie_leader,
                             quorum_tie_candidate, quorum_tie_peer, quorum_tie_over)
from .reserve_tg import (tg_pool_stale, tg_pool_core_lost, tg_pool_ok, _date, webhook_stalled_reserve,
                         webhook_foreign_reserve, webhook_restored_reserve)
from .reserve_rf import rf_transit_on, rf_transit_back, rf_transit_lost, rf_peer_use, rf_peer_done


def peer_down(peer: str, since: str = "") -> str:
    """Т-РУ8: линк пары молчит."""
    at = _when(since)
    return f"🔴 Нет связи с резервом {_n(peer)}" + (f" с {at}" if at else "") + " — пока её нет, пользователи к нему не переедут"


def peer_up(peer: str) -> str:
    return f"🟢 Связь с резервом {_n(peer)} восстановлена"


def zone_tools_failed(reason: str) -> str:
    """Пакеты гостевой зоны (dnsmasq, conntrack) не поставились сами."""
    return (f"⚠️ Для гостевой зоны резерва не поставились пакеты: {_e(reason)} — поставь руками "
            "<code>apt-get install dnsmasq conntrack</code>, иначе пользователей второго сервера не принять")


def pair_unconfirmed(peer: str) -> str:
    """Сопряжение разошлось: пара записана, а сессии на новых ключах не было."""
    return (f"⚠️ Сопряжение с {_n(peer)} не подтвердилось: линк на новых ключах не заработал за 10 минут — "
            "пара снята с этой стороны. Пригласи заново")


def peer_unpaired(peer: str) -> str:
    return f"ℹ️ {_n(peer)} удалил пару: резерва больше нет, его данные с этого сервера сняты"


def direction_off(peer: str, why: str) -> str:
    """Т-РУ9: направление выключилось (версия, возможности)."""
    return f"⚠️ Резерв {_n(peer)}: {_e(why)}"


def direction_on(peer: str) -> str:
    return f"🟢 Резерв {_n(peer)} снова совместим"


def peer_admin_changed(peer: str) -> str:
    return f"ℹ️ У {_n(peer)} сменился администратор — его прежний аккаунт потерял доступ к экрану резерва в этом боте"


__all__ = ["_e", "_n", "_when", "peer_down", "peer_up", "zone_tools_failed", "pair_unconfirmed", "peer_unpaired",
           "direction_off", "direction_on", "peer_admin_changed",
           "hosting_started", "hosting_ended", "hosting_failed", "hosting_without_bot", "return_resumed",
           "moved_to_reserve", "returned_home", "lost_device", "lost_orphan", "primary_back", "move_failed",
           "return_failed", "record_not_changed", "record_own_unknown", "record_manual_needed", "record_missing",
           "record_multi", "record_foreign", "dns_token_rotate", "client_moved", "client_returned", "forced_home",
           "take_tg_stuck", "returned_without_replica", "forced_no_merge", "webhook_stuck_removed",
           "gateway_reserve_down", "gateways_reserve_all_down", "gateway_reserve_up", "gateway_moved",
           "gateway_tg_via", "gateway_move_failed",
           "quorum_code_needed_reserve", "quorum_code_needed_primary", "code_window_closed", "code_wrong",
           "code_open_failed", "_names", "quorum_enabled", "quorum_fenced", "quorum_unfenced", "quorum_enabled_peer",
           "_qn", "_qnames", "_tie_head", "_tie_info", "quorum_tie_leader", "quorum_tie_candidate", "quorum_tie_peer",
           "quorum_tie_over",
           "tg_pool_stale", "tg_pool_core_lost", "tg_pool_ok", "_date", "webhook_stalled_reserve",
           "webhook_foreign_reserve", "webhook_restored_reserve",
           "rf_transit_on", "rf_transit_back", "rf_transit_lost", "rf_peer_use", "rf_peer_done"]
