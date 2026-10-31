"""
outbound_stuck.py — часть пакета reserve (см. __init__): «мои пользователи» —
зависший вебхук резерва (вынесено из outbound).

Право отвечать осталось у вебхука резерва, хотя пользователей он не несёт
(возврат оборвался после передачи бота, резерв перезагрузился без гостя):
запись указывает на меня, а резерв говорит `in = idle` STUCK_IDLE_S подряд или
пары нет на связи дольше STUCK_PAIR_S — `("take_home",)`: `deleteWebhook`,
поллинг, home без возврата, новый `K`. Проверяют такт away и такт home (в home
основа следует по слову Telegram — journal/epoch — и без этого стояла бы без
права записи бессрочно).
"""
from __future__ import annotations

import random

from awgbot.core import config
from awgbot.util import livestep, timeutil

from ..types import Notification
from ..gateway_link.common import log
from .outbound import STUCK_IDLE_S, STUCK_PAIR_S


class ReserveStuckMixin:

    def _webhook_stuck(self, out: dict, now: float) -> bool:
        """Право отвечать осталось у вебхука резерва, хотя пользователей он не несёт:
        запись указывает на меня (проба не чаще раза в 2 мин), а резерв говорит
        `in = idle` (или пары нет на связи дольше STUCK_PAIR_S). Возврат оборвался
        после передачи бота — снять вебхук и поллить самому."""
        if not livestep.mono_due(out.get("stuck_probe"), now, 120 * random.uniform(0.6, 1.4)):
            return False
        self.rsv_update("rsv_out", stuck_probe=now)
        own = self.reserve_own_ip()
        stuck = False
        if own and self.reserve_dns_points_to(own):
            peer = self.reserve_peer_state()
            if self._peer_alive(peer):                        # живым словом, не флагом сеанса (journal/epoch)
                stuck = str(peer.get("inn") or "idle") == "idle"
            else:
                since = str(peer.get("down_since") or "")
                try:
                    stuck = bool(since) and timeutil.elapsed(timeutil.parse_iso(since)) >= STUCK_PAIR_S
                except ValueError:
                    stuck = False
        if not stuck:
            if out.get("stuck_since"):
                self.rsv_update("rsv_out", stuck_since="")
            return False
        since_iso = str(out.get("stuck_since") or "")
        if not since_iso:
            self.rsv_update("rsv_out", stuck_since=timeutil.now_iso())
            return False
        try:
            return timeutil.elapsed(timeutil.parse_iso(since_iso)) >= STUCK_IDLE_S
        except ValueError:
            return False

    def reserve_stuck_home(self) -> list[tuple]:
        """deleteWebhook прошёл (take): пользователи и так здесь — домой без возврата. По
        рычагу «без резерва» (await_merge) — домой с ожиданием его реплики."""
        from awgbot.domain.notices import reserve as rn
        name = self.reserve_peer_name()
        out = self._out()
        forced = bool(out.get("await_merge"))
        self.reserve_set_poll("mine")
        self.reserve_set_out("home", step="", kind=str(out.get("kind") or "") if forced else "",
                             await_merge=forced, last_switch=timeutil.now_iso())
        if self.rsv_get("rsv_qlog").get("adopted"):
            # эпоха принята по кворуму, а пользователи и запись здесь, резерв моих не несёт — отметка
            # принятия снята: роль дома вернёт эпоху сюда (иначе — следование бессрочно, Q8)
            self.rsv_update("rsv_qlog", adopted=None)
            self.__dict__.pop("_quo_adopt_wait_told", None)
        if not forced:
            self.reserve_new_k()
            self.reserve_pkg_mark()
            log.warning("резерв: вебхук «%s» снят — запись на мне, пользователей он не нёс; поллю сам", name)
            return [("live",), ("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.webhook_stuck_removed(name))])]
        journal = bool(self.reserve_jrn_on())                 # пара без журнала — реплика, как прежде
        log.warning("резерв: пользователи возвращены без данных с «%s» — %s, когда он выйдет на связь%s", name,
                    "его изменения перенесутся" if journal else "изменения из его реплики применятся",
                    "; конфликтующие записи будут отброшены" if journal else "")
        return [("live",), ("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.forced_home(name, journal))])]
