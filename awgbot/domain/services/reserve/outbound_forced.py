"""
outbound_forced.py — часть пакета reserve (см. __init__): «мои пользователи» —
срок шага переезда кнопкой, а бот уже у гостя принудительным захватом (вынесено
из outbound).

Основа умерла между `up` и `released`: резерв не дождался передачи бота, увидел
её «не на связи» и отдал Telegram гостю сам (`tg_take_forced`, inbound) — клон уже
несёт пользователей, гость правит её данные. Ожившая основа на шаге переезда
видит вышедший срок; прежний срыв (`_move_fail`: запись обратно на себя, `back`)
снимал гостя вместе с правками. Теперь: живой `st` резерва — `in = serving`,
`in_tg = forced` — переезд состоялся, away (как ветка «вебхук у резерва» шага
released), возврат — обычным путём, со слиянием. `st` резерва ещё прежний (после
перезапуска основы или потери связи, `st_stale`) — подождать свежий не дольше
FORCED_ST_WAIT_S от первого замеченного срока, а не решать по старому взгляду.
"""
from __future__ import annotations

import time

from awgbot.core import config
from awgbot.util import timeutil

from ..types import Notification
from ..gateway_link.common import log

FORCED_ST_WAIT_S = 60             # срок шага вышел, а st резерва прежний — ждать свежий не дольше


class ReserveForcedTakeMixin:

    def _leaving_forced(self, out: dict, step: str) -> list[tuple] | None:
        """Срок шага `step` переезда вышел. None — решать как прежде (срыв); список — решено
        здесь: away по живому слову резерва «бот у меня принудительно» или ожидание свежего `st`."""
        if step not in ("up", "record", "release") or not out.get("go_sent"):
            return None                                       # K резерву не ушёл — открыть пакет ему нечем
        peer = self.reserve_peer_state()
        if not self._peer_alive(peer):
            key = f"{step}|{out.get('step_at') or ''}"
            seen = self.__dict__.setdefault("_leave_exp_seen", {})
            first = seen.setdefault(key, time.monotonic())
            if time.monotonic() - first < FORCED_ST_WAIT_S:
                return []                                     # st резерва прежний — свежий скажет, не забрал ли гость бота
            return None
        if str(peer.get("inn") or "") != "serving" or str(peer.get("in_tg") or "") != "forced":
            return None
        return self._leaving_to_away_forced(out, step)

    def _leaving_to_away_forced(self, out: dict, step: str) -> list[tuple]:
        from awgbot.domain.notices import reserve as rn
        # под замком и только с того же шага: слово канала за это время могло увести автомат
        if not self._out_cas("leaving", (step,), to="away", kind=str(out.get("kind") or "button"), step="",
                             taken_at=timeutil.now_iso(), last_switch=timeutil.now_iso(), healthy_since=""):
            return []
        self.reserve_set_poll("away")
        log.warning("резерв: срок шага %s вышел, а бот у «%s» по принудительному захвату (я не отдал released и "
                    "пропал) — away, возврат обычным путём", step, self.reserve_peer_name())
        return [("live",), ("notify", [Notification(tg_id=int(config.ADMIN_ID), text=rn.moved_to_reserve(
            self.reserve_peer_name(), str(out.get("kind") or "button"), timeutil.now_iso(),
            dead_since=str(out.get("dead_since") or ""), reserve_ip=str(out.get("target_ip") or "")))])]
