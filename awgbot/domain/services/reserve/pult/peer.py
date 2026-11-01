"""
peer.py — часть пакета pult (см. __init__): сторона резерва (у меня пользователи
основы, её бот — гостем в зоне).

`st.tg_pult` основы — Telegram её бота у неё (пульт), пока её пользователи у меня.
Тогда гостю вебхук не ставить (`reserve_pult_hold_why` — условие запрета; оно же
уходит гостю полем `tg_hold` слова `state`), пока основа не отдаст Telegram словом
`pult_back` с той же отметкой: бот резерва запоминает отметку и велит гостю снова
поставить вебхук (`tg_retake` по локальному каналу). Ожил и увидел отметку — админу
резерва одно уведомление на отметку.
"""
from __future__ import annotations

from awgbot.core import config

from ...gateway_link.common import log
from ...types import Notification
from ..state import HOSTING


class ReservePultPeerMixin:

    def reserve_pult_hold_why(self, inn: dict | None = None) -> str:
        """Причина не ставить гостю вебхук по пульту основы; пусто — пульт не мешает. Отметку в
        `st` основы (её последнем, и устаревшем тоже — свежий скажет иное) снимает только слово
        `pult_back` с той же отметкой или её новый `st` без неё."""
        at = str(self.reserve_peer_state().get("tg_pult") or "")
        if not at:
            return ""
        inn = self.rsv_get("rsv_in") if inn is None else inn
        if str(inn.get("pult_back") or "") == at:
            return ""
        return "Telegram бота основы у неё — жду, пока отдаст"

    def reserve_pult_back_in(self, at: str) -> list[tuple]:
        """`pult_back{at}` основы: она отдаёт Telegram — гостю поставить вебхук снова."""
        inn = self.rsv_get("rsv_in")
        name = self.reserve_peer_name()
        if str(inn.get("state") or "idle") not in ("taking", "serving"):
            log.info("резерв: «%s» отдаёт Telegram, а её пользователей у меня нет — пропущено", name)
            return []
        # отметка «вебхук не здесь» гостя (её снял пульт основы) — прошлая: следующую снова сказать админу
        self.rsv_update("rsv_in", pult_back=str(at or ""), webhook_foreign=False)
        log.warning("резерв: «%s» отдаёт Telegram своего бота — гостю снова ставить вебхук", name)
        return [("guest", "tg_retake", {})]

    def reserve_pult_seen_notes(self) -> list[Notification]:
        """`st` основы с отметкой пульта, пока её пользователи у меня: админу резерва — один раз на
        отметку (пульт отдан и снова взят — новая отметка, новое слово)."""
        from awgbot.domain.notices import reserve_pult as rp
        at = str(self.reserve_peer_state().get("tg_pult") or "")
        if not at:
            return []
        inn = self.rsv_get("rsv_in")
        if str(inn.get("state") or "idle") not in HOSTING or at in (str(inn.get("pult_told") or ""),
                                                                     str(inn.get("pult_back") or "")):
            return []
        self.rsv_update("rsv_in", pult_told=at)
        name = self.reserve_peer_name()
        log.warning("резерв: Telegram бота «%s» у неё (пульт), её пользователи у меня — гостю вебхук не ставлю", name)
        return [Notification(tg_id=int(config.ADMIN_ID), force_sound=True, text=rp.pult_at_primary(name))]
