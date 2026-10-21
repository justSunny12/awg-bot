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

    def reserve_main_status(self) -> dict:
        """Т-Р17: статус резерва для шапки главной — {name, dot, text}; пусто — пары нет.
        Как у строки сервера: имя, кружок, короткая суть. 🟢 — переехать можно прямо
        сейчас (связь, направление готово, данные для переезда у резерва) или резерв
        несёт клиентов; 🟡 — не готов, с причиной, либо ход переезда/возврата;
        🔴 — недоступен физически (линк пары молчит)."""
        if not self.reserve_paired():
            return {}
        name = self.reserve_peer_name()
        out, inn = self.rsv_get("rsv_out"), self.rsv_get("rsv_in")
        o, i = str(out.get("state") or "home"), str(inn.get("state") or "idle")

        def st(dot: str, text: str) -> dict:
            return {"name": name, "dot": dot, "text": text}

        if o == "leaving":
            return st("🟡", "твои клиенты переезжают — жду смену записи")
        if o == "away":
            return st("🟢", f"несёт твоих клиентов и бот с {_when(str(out.get('taken_at') or out.get('since') or ''))}")
        if o == "returning":
            return st("🟡", "твои клиенты возвращаются на сервер")
        if o == "home" and out.get("relay_until") and i == "idle":
            try:
                if timeutil.parse_iso(str(out["relay_until"])) > timeutil.now():
                    return st("🟡", f"твои клиенты возвращаются на сервер — пересылка до {_date(str(out['relay_until']))}")
            except ValueError:
                pass
        if i in ("taking", "serving", "releasing"):
            return st("🟢", f"чужие клиенты и бот у тебя с {_when(str(inn.get('taken_at') or inn.get('since') or ''))}")
        if i == "relay":
            return st("🟡", f"чужие клиенты возвращаются на резерв — пересылка до {_date(str(inn.get('relay_until') or ''))}")
        if self.reserve_peer_seen() == "down":
            return st("🔴", f"недоступен с {_when(self.reserve_down_since())}")
        if not self.reserve_online():
            # сессия была и оборвалась — «недоступен» скажет проба; не было ни разу — так и говорим
            return st("🟡", "сейчас не на связи" if self.reserve_peer_state().get("session_at") else "связи ещё не было")
        ok, why = self.reserve_directions()["out"]
        pkg = self.rsv_get("rsv_pkg_sent")
        if ok and not pkg.get("at"):
            ok, why = False, "данных для переезда ещё нет"
        if not ok:
            return st("🟡", f"не готов: {self._main_reason(why, name)}")
        return st("🟢", f"резерв готов · данные от {_when(str(pkg.get('at') or ''))}")

    @staticmethod
    def _main_reason(why: str, name: str) -> str:
        """Причина для строки главной: имя резерва в строке уже есть, сторона — «у тебя»."""
        why = why.replace("здесь выключено", "у тебя выключено").replace(f"у «{name}» выключено", "выключено")
        why = why.replace(f"у «{name}»: ", "").replace("здесь: ", "у тебя: ")
        why = why.replace(f"у «{name}» v", "v").replace(", здесь v", ", у тебя v")          # версии: «v4.0.0, у тебя v4.1.0 — …»
        if why.startswith(f"«{name}» "):                      # «„Beta“ не читает формат…», «… не умеет принимать…»
            why = why[len(f"«{name}» "):]
        return why.replace("у резерва устарели", "устарели")

    def reserve_main_line(self) -> str:
        """Та же строка одним текстом (журнал, тесты): «🛟 Beta 🟢 резерв готов · …»."""
        s = self.reserve_main_status()
        return f"🛟 {s['name']} {s['dot']} {s['text']}" if s else ""

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
