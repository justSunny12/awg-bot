"""
follow.py — часть пакета journal (см. __init__): основа в следовании.

Пока решения пишет бот основы на резерве, у основы остаются сессии
пользователей, ещё не переподключившихся к резерву. Их блокировки решает
держатель: бот основы на резерве шлёт адреса заблокированных устройств
(сообщение `blocks`, обратное прежнему), основа держит DROP по ним, а не по
своей устаревшей базе. Месячный сброс в следовании откладывается: его делает
держатель, а после возврата со слиянием он уже учтён (`reserve_monthly_settle`).
"""
from __future__ import annotations

from awgbot.util import timeutil

from ...gateway_link.common import log
from ..away import AWAY_REFUSED
from ...types import ServiceError


class ReserveFollowMixin:

    # ── блокировки держателя ────────────────────────────────────────────────
    def reserve_holder_blocks_in(self, addrs: list[str]) -> None:
        """`blocks` от бота основы на резерве: DROP оставшимся здесь сессиям по его решениям."""
        if not self.reserve_following():
            log.info("резерв: блокировки от «%s» вне следования — пропущены", self.reserve_peer_name())
            return
        clean = sorted({str(a) for a in addrs if a})[:4096]
        self.rsv_update("rsv_follow", blocks=clean, blocks_at=timeutil.now_iso())
        try:
            self.reconcile_blocks(True)
        except Exception as e:                                # noqa: BLE001
            log.warning("резерв: блокировки держателя не наложены: %s", e)

    def reserve_holder_blocks(self) -> list[str] | None:
        """Адреса для DROP в следовании; None — не следование или держатель их ещё не прислал
        (тогда — по своей базе: решения в ней с начала следования не меняются)."""
        if not self.reserve_following():
            return None
        f = self.rsv_get("rsv_follow")
        if not f.get("blocks_at"):
            return None
        return [str(a) for a in (f.get("blocks") or [])]

    # ── месячный сброс ──────────────────────────────────────────────────────
    def reserve_monthly_defer(self, ym: str) -> None:
        """Сброс месяца пришёлся на следование: его сделал держатель."""
        if str(self.rsv_get("rsv_follow").get("monthly_deferred") or "") < ym:
            self.rsv_update("rsv_follow", monthly_deferred=ym)
            log.info("резерв: месячный сброс %s — у бота на «%s»", ym, self.reserve_peer_name())

    def reserve_monthly_settle(self) -> None:
        """Слияние реплики прошло: сброс, сделанный держателем, учтён — отметка месяца
        здесь (иначе догон на старте сбросил бы месяц второй раз)."""
        ym = str(self.rsv_get("rsv_follow").get("monthly_deferred") or "")
        if ym and str(self.db.get_state("last_monthly_reset") or "") < ym:
            self.db.set_state("last_monthly_reset", ym)
            log.info("резерв: месячный сброс %s учтён слиянием", ym)
        if ym:
            self.rsv_update("rsv_follow", monthly_deferred="")

    def reserve_follow_clear(self) -> None:
        """Конец следования: блокировки держателя прочь; отложенный месячный сброс
        остаётся — его учитывает слияние после возврата (reserve_monthly_settle)."""
        f = self.rsv_get("rsv_follow")
        self.rsv_put("rsv_follow", {"monthly_deferred": f["monthly_deferred"]} if f.get("monthly_deferred") else None)

    # ── отказ базы ──────────────────────────────────────────────────────────
    def reserve_refusal(self, exc: BaseException) -> ServiceError | None:
        """Отказ базы в записи решения (следование) — словами раздела; не отказ — None."""
        from awgbot.util import rsvjrn
        if rsvjrn.is_refusal(exc):
            return ServiceError(AWAY_REFUSED.format(name=self.reserve_peer_name()))
        return None
