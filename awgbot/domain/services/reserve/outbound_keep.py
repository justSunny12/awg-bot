"""
outbound_keep.py — часть пакета reserve (см. __init__): «мои пользователи» —
приём у резерва сорвался после того, как его процесс открытия сменил мою запись
DNS на резерв (сторона резерва — inbound_keep).

Резерв зону при этом не снимает и доделывает сам; его `st` несёт поле
`in_keep{k, why, at}`, пока не доделано, — я на связи: своему админу громкое
уведомление о сбое с причиной, один раз на эпизод (по `at`).

Клона у резерва нет, а я жив и на связи — он кончает приём и шлёт `down{rec}`:
запись на нём, вернуть её — мне. Возвращаю не сразу по слову, а по живому `st`
резерва после него (`_peer_alive`, `st` позже отметки): резерв моих не несёт —
запись на себя (API — если автоматически; «вручную» — просьба админу), несёт снова
(открыл заново) — отметка снимается, запись не трогаю. Без живого `st` отметка ждёт:
мёртвому или молчащему резерву запись не отдаётся и не отнимается.
"""
from __future__ import annotations

import time

from awgbot.core import config

from ..types import Notification, ServiceError
from ..gateway_link.common import log
from .state import HOSTING


class ReserveOutKeepMixin:

    def reserve_keep_st_in(self, part) -> list[Notification]:
        """`st.in_keep` резерва: новый эпизод (`at`) — уведомление моему админу."""
        from awgbot.domain.notices import reserve_keep as rk
        if not isinstance(part, dict) or not part.get("at"):
            return []
        at = str(part.get("at") or "")[:40]
        if str(self.rsv_get("rsv_peer_state").get("keep_told_at") or "") == at:
            return []
        self.rsv_update("rsv_peer_state", keep_told_at=at)
        kind, why = str(part.get("k") or "")[:16], str(part.get("why") or "")[:120]
        log.warning("резерв: у «%s» приём моих пользователей сорвался после смены записи (%s): %s",
                    self.reserve_peer_name(), kind, why)
        return [Notification(tg_id=int(config.ADMIN_ID), force_sound=True,
                             text=rk.peer_kept(self.reserve_peer_name(), kind, why))]

    def reserve_rec_back_mark(self, reason: str) -> list[tuple]:
        """`down{rec}` резерва: запись на нём, а моих он больше не примет — вернуть запись на
        себя по его живому `st` после этой отметки (`reserve_rec_back_settle`)."""
        self.rsv_update("rsv_out", rec_back={"why": str(reason)[:120], "at_ts": time.time()})
        log.warning("резерв: «%s» не принял моих после смены записи на себя: %s — запись верну по его живому st",
                    self.reserve_peer_name(), reason)
        return []

    def reserve_rec_back_settle(self) -> list[tuple]:
        """Такт home: отметка `rec_back` и живой `st` резерва после неё."""
        out = self._out()
        rb = out.get("rec_back") if isinstance(out.get("rec_back"), dict) else None
        if not rb:
            return []
        peer = self.reserve_peer_state()
        try:
            fresh = float(peer.get("at_ts") or 0) > float(rb.get("at_ts") or 0)
        except (TypeError, ValueError):
            fresh = False
        if not fresh or not self._peer_alive(peer):
            return []
        self.rsv_update("rsv_out", rec_back=None)
        name = self.reserve_peer_name()
        if str(peer.get("inn") or "idle") in HOSTING:
            log.info("резерв: «%s» снова принимает моих — запись не трогаю", name)
            return []
        return [("live",)] + self._rec_back_switch(str(rb.get("why") or ""))

    def _rec_back_switch(self, why: str) -> list[tuple]:
        from awgbot.domain.notices import reserve as rn
        from awgbot.domain.notices import reserve_keep as rk
        record, own = self.reserve_dns_name(), self.reserve_own_ip()
        if not record:
            return []
        if not own:
            return [("notify", [self._own_unknown_note()])]
        if not self.reserve_dns_automatic():
            return [("notify", [Notification(tg_id=int(config.ADMIN_ID), force_sound=True,
                                             text=rn.record_manual_needed(record, own, back=True))])]
        try:
            self.reserve_dns_switch(own)
        except ServiceError as e:
            return [("notify", [Notification(tg_id=int(config.ADMIN_ID), force_sound=True,
                                             text=rn.record_not_changed(record, str(e), own))])]
        log.warning("резерв: запись %s возвращена на себя (%s) — «%s» моих не принял: %s", record, own,
                    self.reserve_peer_name(), why)
        return [("notify", [Notification(tg_id=int(config.ADMIN_ID), force_sound=True,
                                         text=rk.record_back(self.reserve_peer_name(), record, own, why))])]
