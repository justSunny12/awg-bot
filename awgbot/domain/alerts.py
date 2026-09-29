"""
alerts.py — гистерезис алертов по стрикам, общий для основного бота и агента.

Алерт — после N плохих замеров ПОДРЯД, отбой — после N хороших; счётчики с
потолком (выше порога они ничего не решают, а без потолка каждый спокойный
тик был бы записью на диск). «Взведён» переключается только ПОСЛЕ доставки
(Notification.on_sent): запись до отправки давала одинокий отбой — сеть падает
вместе с бедой, алерт не улетал, а «✅ снова в норме» приходил первым и
единственным словом.
"""
from __future__ import annotations

from awgbot.core import config
from awgbot.domain.services.types import Notification


def armer(db, armed_key: str, value: str):
    """Отложенная отметка «алерт показан» — её ставит рассылка по факту доставки."""
    def _mark() -> None:
        db.set_state(armed_key, value)
    return _mark


def streak_alert(db, keys: tuple[str, str, str], bad: bool | None, streak: int,
                 on_text: str, off_text: str, *, loud: bool = True, critical: bool = True,
                 chat_id: int | None = None) -> list[Notification]:
    """keys — (счётчик плохих, счётчик хороших, «взведён»). None не двигает
    счётчики: «не смог посмотреть» — не норма и не отказ."""
    if bad is None:
        return []
    hi_key, lo_key, armed_key = keys
    hi = int(db.get_state(hi_key) or 0)
    lo = int(db.get_state(lo_key) or 0)
    armed = db.get_state(armed_key) == "1"
    to = config.ADMIN_ID if chat_id is None else chat_id
    notes: list[Notification] = []
    if bad:
        hi, lo = min(hi + 1, streak), 0
        if hi >= streak and not armed:
            notes.append(Notification(to, on_text, force_sound=loud, critical=critical,
                                      on_sent=armer(db, armed_key, "1")))
    else:
        lo, hi = min(lo + 1, streak), 0
        if lo >= streak and armed:
            notes.append(Notification(to, off_text, on_sent=armer(db, armed_key, "0")))
    db.set_state(hi_key, str(hi))
    db.set_state(lo_key, str(lo))
    return notes


__all__ = ["streak_alert", "armer"]
