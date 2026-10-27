"""claim.py — claim по каналу."""
from __future__ import annotations


from awgbot.domain.services.types import ServiceError

from .common import log


class ClaimMixin:
    # ── claim по каналу ──────────────────────────────────────────────────────

    def gwlink_claim_in(self, slot_id: int, token: str) -> None:
        """Агент прислал токен пометки своим каналом. Разбор — gateway_claim,
        включая список нонсов."""
        try:
            res = self.gateway_claim(token)
        except (ValueError, ServiceError) as e:
            log.info("канал линка: claim слота %s не принят: %s", slot_id, e)
            return
        # Токен подписан ключом линка — значит, своего слота; но проверим явно:
        # сообщение из сессии слота 1 не должно метить устройство слота 2.
        got = res.get("gateway") if isinstance(res, dict) else None
        if got is not None and getattr(got, "id", slot_id) != slot_id:
            log.warning("канал линка: claim из сессии слота %s пометил слот %s", slot_id, got.id)
