"""notes.py — тексты уведомлений маршрутизации."""

from __future__ import annotations

from awgbot.core import settings
from awgbot.infra import routing
from awgbot.util import timeutil


class RoutingNotesMixin:
    """Тексты уведомлений маршрутизации."""
    # ── тексты уведомлений ───────────────────────────────────────────────────
    def _txt_rt_gw_up(self, active=None) -> str:
        who = f" {self._gw_link(active)}" if active is not None else ""
        return f"🟢 Шлюз{who} снова в строю"

    def _txt_rt_switched(self, prev, new, verdict: str) -> str:
        head = (f"🔁 Шлюз переключён: {self._gw_link(prev) if prev else 'прежний'} не отвечает, "
                f"трафик идёт через {self._gw_link(new)}.\n\n"
                "Исходящий адрес у клиентов сменился — российские приложения могут "
                f"попросить войти заново. Останусь на {self._gw_display_h(new)} и после того, как "
                f"{self._gw_display_h(prev) if prev else 'прежний'} оживёт; вернуть трафик "
                f"обратно можно {self._gw_card_link(new)}.")
        if verdict == routing.PROBE_NO_PATH:
            head += "\n\nТуннель до него жив — проверь аплинк и NAT на самом шлюзе"
        else:
            head += self._txt_rt_bundle_hint(prev)
        return head

    def _txt_rt_switch_refused(self, active) -> str:
        raw = self.db.get_state(self._RT_SWITCHED_KEY) or ""
        try:
            mins = max(1, int((timeutil.now() - timeutil.parse_iso(raw)).total_seconds() // 60))
        except ValueError:
            mins = settings.get_int("app.routing.failover.min_interval_minutes", 10)
        return (f"🔴 {self._gw_link(active)} перестал отвечать через {mins} мин после "
                "переключения на него. Второе переключение подряд не делаю: проблема выглядит "
                "системной. " + self._rt_effect_line()
                + f"\n\nПереключить принудительно можно {self._gw_card_link(active)}")

    def _txt_rt_standby_also_down(self, dead) -> str:
        names = ", ".join(self._gw_link(g) for g in dead)
        return f"Резервный {names} тоже не отвечает."

    def _txt_rt_standby_down(self, g, active, ticks: int) -> str:
        mins = max(1, ticks * settings.get_int("app.routing.probe_seconds", 30) // 60)
        via = f"трафик идёт через {self._gw_display_h(active)}" if active else "трафик не затронут"
        return (f"⚠️ Резервный шлюз {self._gw_link(g)} не отвечает уже {mins} мин — "
                f"резерва сейчас нет. Клиенты не затронуты: {via}")

    def _txt_rt_standby_up(self, g, active) -> str:
        via = f". Трафик идёт через {self._gw_display_h(active)}" if active else ""
        return f"🟢 Резервный шлюз {self._gw_link(g)} снова отвечает — остаётся в резерве{via}"
