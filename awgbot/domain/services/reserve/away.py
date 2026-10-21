"""
away.py — часть пакета reserve (см. __init__): «клиенты у резерва» глазами
основы — отказы действий над хостом, пока клиенты не здесь (набор
обновлений, переезд профилей, восстановление, обновление: они разошлись бы
с репликой) и пока здесь клиенты и бот второго сервера (taking, serving,
releasing: код и данные менялись бы под гостем; в терминале то же решает
tools/rsvbusy.py), строка главного экрана админа (Т-Р17) и данные гостю по
локальному каналу (на стороне резерва — reserve_guest_state_view в guest.py).
"""
from __future__ import annotations

from awgbot.core import settings
from awgbot.util import timeutil

from ..types import ServiceError

HOSTING_REFUSED = "Сейчас здесь клиенты и бот «{name}» — сначала их возврат к нему"
AWAY_REFUSED = ("Клиенты и бот сейчас на резерве «{name}» — действия над этим сервером отложены до возврата, "
                "иначе изменения разойдутся с тем, что происходит на резерве")


class ReserveAwayMixin:
    def reserve_refuse_if_away(self) -> None:
        """В начале действий над хостом: пока мои клиенты у резерва — отказ; пока
        у меня клиенты и бот резерва — тоже (код и данные менялись бы под гостем)."""
        if not self.reserve_paired():
            return
        if self.reserve_away():
            raise ServiceError(AWAY_REFUSED.format(name=self.reserve_peer_name()))
        if self.reserve_in() in ("taking", "serving", "releasing"):
            raise ServiceError(HOSTING_REFUSED.format(name=self.reserve_peer_name()))

    def reserve_main_line(self) -> str:
        """Т-Р17: строка главной админа о резерве — особое состояние (переезд, возврат,
        недоступен) или короткое «на связи»; пусто — пары нет."""
        if not self.reserve_paired():
            return ""
        name = self.reserve_peer_name()
        out, inn = self.rsv_get("rsv_out"), self.rsv_get("rsv_in")
        o, i = str(out.get("state") or "home"), str(inn.get("state") or "idle")
        if o == "leaving":
            return f"🛟 Клиенты переезжают на резерв «{name}» — жду смену записи"
        if o == "away":
            return f"🛟 Клиенты и бот — на «{name}» с {_when(str(out.get('taken_at') or out.get('since') or ''))}"
        if o == "returning":
            return f"🛟 Клиенты возвращаются с резерва «{name}»"
        if o == "home" and out.get("relay_until") and i == "idle":
            try:
                if timeutil.parse_iso(str(out["relay_until"])) > timeutil.now():
                    return f"🛟 Клиенты возвращаются с резерва «{name}» — пересылка до {_date(str(out['relay_until']))}"
            except ValueError:
                pass
        if i in ("taking", "serving", "releasing"):
            return f"🛟 У тебя клиенты и бот «{name}» с {_when(str(inn.get('taken_at') or inn.get('since') or ''))}"
        if i == "relay":
            return f"🛟 Клиенты «{name}» возвращаются к нему — пересылка до {_date(str(inn.get('relay_until') or ''))}"
        if self.reserve_peer_seen() == "down":
            return f"🔴 Резерв «{name}» недоступен с {_when(self.reserve_down_since())}"
        # обычное состояние: резерв есть — коротко, что он на связи и когда у него данные для переезда
        ready = settings.get_bool("app.reserve.ready", False)
        pkg = self.rsv_get("rsv_pkg_sent")
        if self.reserve_online() and ready and pkg.get("at"):
            return f"🛟 Резерв «{name}» на связи · данные для переезда от {_when(str(pkg.get('at') or ''))}"
        if self.reserve_online():
            return f"🛟 Резерв «{name}» на связи" + ("" if ready else " · готовность выключена")
        return f"🛟 Резерв «{name}»: связи ещё не было"

    def reserve_blocks_to_send(self) -> list[str] | None:
        """Пока мои клиенты у резерва: адреса заблокированных устройств — ему
        (бот основы на резерве наложит DROP в зоне); None — не изменились."""
        if not self.reserve_paired() or not self.reserve_away():
            self.__dict__.pop("_rsv_blocks_sent", None)
            return None
        addrs = sorted({str(d.address) for d in self.db.list_all_devices()
                        if getattr(d, "block_reason", 0) and d.address})
        if self.__dict__.get("_rsv_blocks_sent") == addrs:
            return None
        self.__dict__["_rsv_blocks_sent"] = addrs
        return addrs

    def reserve_return_lever_button(self) -> bool:
        return str(settings.get("app.reserve.return", "immediate") or "immediate") == "button"


def _when(iso: str) -> str:
    try:
        return timeutil.fmt_clock_ui(timeutil.parse_iso(iso))
    except (ValueError, TypeError):
        return "?"


def _date(iso: str) -> str:
    try:
        return timeutil.fmt_dt_ui(timeutil.parse_iso(iso))
    except (ValueError, TypeError):
        return "?"
