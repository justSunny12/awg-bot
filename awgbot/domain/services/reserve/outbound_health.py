"""
outbound_health.py — часть пакета reserve (см. __init__): «мои пользователи» —
away: здоровье основы и возврат сам (вынесено из outbound). Здорова ≥ HEALTHY_S
(свой AWG, линк пары, шлюз, Telegram достижим с этого сервера; под кворумом —
счёт Q2) → по рычагу «сразу» возврат сам (не чаще min_interval_minutes; после
повторной той же ошибки слияния — `merge_blocked`), «по кнопке» — Т-РУ6 через
бот основы на резерве. Сорванное «забираю» кворума (`_share_remainder`) и
остаток вебхука у живого резерва без моих — домой с прежними отметками home.
"""
from __future__ import annotations

import time

from awgbot.core import settings
from awgbot.util import timeutil

from ..gateway_link.common import log
from .outbound import HEALTHY_S
from .state import HOSTING, _LOCK


class ReserveOutHealthMixin:
    # ── away: здоровье и возврат ────────────────────────────────────────────
    def reserve_healthy(self) -> bool:
        """Свой AWG отвечает, линк пары жив, шлюз (если есть) на связи — сейчас. Под
        кворумом «свой AWG отвечает» и «шлюз на связи» заменяет счёт Q2: «да» за меня
        путём пользователей строго больше половины голосующих."""
        q = self.reserve_quorum_healthy() if hasattr(self, "reserve_quorum_healthy") else None
        if q is not None:
            return bool(q and self.reserve_online() and self.reserve_peer_seen() == "ok" and self._tg_reachable())
        try:
            awg_ok = str(self.db.get_state("last_server_ok") or "") == "1"
        except Exception:                                     # noqa: BLE001
            awg_ok = False
        pair_ok = self.reserve_online() and self.reserve_peer_seen() == "ok"
        gw_ok = True
        if self.reserve_has_gateway() and hasattr(self, "gwlink_session"):
            gw_ok = any(bool(self.gwlink_session(g.id)) for g in self.db.gateways())
        return bool(awg_ok and pair_ok and gw_ok and self._tg_reachable())

    def _quorum_adopted_hosting_seen(self) -> None:
        """Резерв после принятия эпохи несёт (нёс) моих пользователей — отметка в записи принятия:
        его позднейшее «idle» — уже не «забираю сорвалось», а возврат, который идёт обычным путём
        (запись DNS на его адресе, вебхук гостя, его правки) — домой по одному слову idle нельзя."""
        adopted = dict(self.rsv_get("rsv_qlog").get("adopted") or {})
        if adopted and not adopted.get("hosting_seen"):
            self.rsv_update("rsv_qlog", adopted={**adopted, "hosting_seen": True})

    def _share_remainder(self, out: dict) -> bool:
        """В away по принятой эпохе кворума (кандидат сказал голосующим «забираю»), а живая вторая
        сторона после принятия говорит, что моих не несёт: «забираю» сорвалось (своё «вижу»
        вернулось, открытие не пошло) — пользователи никуда не уезжали. Только пока резерв после
        принятия моих не нёс (`hosting_seen`): нёс — его idle означает снятую зону после обслуживания,
        запись на его адресе, и домой ведёт обычный возврат. Без отметки принятия — не этот случай."""
        if str(out.get("by") or "") != "share" or not hasattr(self, "_quorum_adopted_idle_fact"):
            return False
        adopted = dict(self.rsv_get("rsv_qlog").get("adopted") or {})
        if not adopted or adopted.get("hosting_seen"):
            return False
        peer = self.reserve_peer_state()
        hosting = str(peer.get("inn") or "idle") in HOSTING
        if hosting and (peer.get("at_ts") is None or adopted.get("at_ts") is None or float(peer["at_ts"]) > float(adopted["at_ts"])):
            self._quorum_adopted_hosting_seen()               # несёт моих — отметка, выхода по idle больше нет
            return False
        if hasattr(self, "reserve_disk_waiting") and self.reserve_disk_waiting():
            return False                                      # на диске резерва журнал гостя новее — переезд был
        return self._quorum_adopted_idle_fact(adopted)

    def _tick_away(self, out: dict) -> list[tuple]:
        now = time.monotonic()
        if self._share_remainder(out):
            # домой с прежними отметками home (как остаток вебхука ниже); эпоху, если её ещё держит вторая
            # сторона, вернёт правило лидера — факт «моих у неё нет» запоминается в отметке принятия
            with _LOCK:
                cur = dict(self.rsv_get("rsv_out"))
                cur.update(state="home", kind="", step="", by="", await_merge=False, since=str(cur.get("home_since") or ""))
                cur.pop("at_ts", None)
                if cur.get("home_at_ts") is not None:
                    cur["at_ts"] = cur["home_at_ts"]
                cur.pop("home_since", None)
                cur.pop("home_at_ts", None)
                self.rsv_put("rsv_out", cur)
                adopted = dict(self.rsv_get("rsv_qlog").get("adopted") or {})
                if adopted:
                    self.rsv_update("rsv_qlog", adopted={**adopted, "idle_seen": True})
            self.reserve_set_poll("mine")
            self.reserve_epoch_sync()
            log.warning("резерв: «%s» на связи без моих пользователей — «забираю» сорвалось, я дома", self.reserve_peer_name())
            notes = self.reserve_compromised_settle()         # точки ключа были у него (st.exposed) — поколение сейчас
            return [("live",), ("wake_poll",)] + ([("notify", notes)] if notes else [])
        if str(out.get("by") or "") == "webhook" and self._webhook_remainder(out, self.reserve_peer_state()):
            # в away по одному слову Telegram, а живой резерв после перехода говорит, что моих не несёт:
            # остаток вебхука — домой, запись здесь; сам вебхук снимет проверка зависшего (outbound_stuck)
            # отметки home — прежние (home_since/home_at_ts): как будто автомат и не уходил, роль и
            # проверка зависшего вебхука считают от них
            with _LOCK:
                cur = dict(self.rsv_get("rsv_out"))
                cur.update(state="home", kind="", step="", by="", await_merge=False, since=str(cur.get("home_since") or ""),
                           webhook_rem_ts=self.rsv_get("rsv_poll").get("at_ts"))   # это слово Telegram — остаток, второй раз не ведёт
                cur.pop("at_ts", None)
                if cur.get("home_at_ts") is not None:
                    cur["at_ts"] = cur["home_at_ts"]
                cur.pop("home_since", None)
                cur.pop("home_at_ts", None)
                self.rsv_put("rsv_out", cur)
            self.reserve_epoch_sync()
            log.warning("резерв: «%s» на связи без моего гостя — вебхук у него остаток, я дома", self.reserve_peer_name())
            notes = self.reserve_compromised_settle()         # открывал без моего K (его st.exposed) — поколение сейчас
            return [("live",)] + ([("notify", notes)] if notes else [])
        if self.reserve_poll() == "away" and self._webhook_stuck(out, now):
            return [("take_home",)]
        if self.reserve_healthy():
            since = float(out.get("healthy_mono") or 0)
            if not since or since > now + 1.0:                # отметки нет или она прежней загрузки — счёт заново
                self.rsv_update("rsv_out", healthy_mono=now, healthy_since=timeutil.now_iso())
                return []
            if now - since < settings.get_int("reserve.healthy_seconds", HEALTHY_S):
                return []
            if out.get("merge_blocked"):
                # слияние дважды упало с той же причиной — сам не повторяю, но кнопка «↩️ Вернуть пользователей»
                # (у основы и у гостя) должна быть: can_return держится, пока основа здорова
                if not out.get("can_return"):
                    self.rsv_update("rsv_out", can_return=True)
                    return [("live",)]
                return []
            if self.reserve_return_mode() == "immediate" \
                    and not self.reserve_return_check():
                last = str(out.get("last_switch") or "")
                gap = settings.get_int("reserve.min_interval_minutes", 30) * 60
                if not last or timeutil.elapsed(timeutil.parse_iso(last)) >= gap:
                    return self.reserve_return_start("auto")
            elif not out.get("can_return"):
                self.rsv_update("rsv_out", can_return=True)
                return [("live",)]
        elif out.get("healthy_mono") or out.get("can_return"):
            self.rsv_update("rsv_out", healthy_mono=0, healthy_since="", can_return=False)
        return []
