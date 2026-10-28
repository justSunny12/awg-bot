"""
c9.py — часть пакета journal (см. __init__): «вернуть без резерва» (С9) —
правки бота на резерве хвостом.

Основа взяла эпоху сама (`reserve_c9_start`): события бота на резерве эпохи,
которую она сменила, приходят при возврате его же записями (живой гость или
его журнал данных с диска резерва) и перебазируются здесь при получении
(`reserve_c9_tail_in`, подтверждение — `jack`); записи запечатаны прежним `K` —
он отложен в отметке `rsv_c9`. Пока ждём эти правки (`rsv_out.await_merge`) —
обновление отказывает (tail `reserve_refuse_update_if_waiting`, сторож
терминала — `c9_wait_raw` в tail). Итог — Т-Ж4/Т-Ж5 после возврата
(`reserve_c9_notes`); перенос, строки и уведомления — общие с хвостом (tail).
"""
from __future__ import annotations

import json

from awgbot.util import timeutil

from ...gateway_link.common import log
from ...types import Notification
from .tail import DISPUTED_MAX


class ReserveC9Mixin:

    def reserve_c9_start(self, guest_e: int) -> None:
        """Основа взяла эпоху без резерва: события бота на резерве эпохи guest_e после моей
        точки взятия эпохи — хвост; они придут его же записями (живой гость при возврате или
        журнал данных гостя с диска резерва) и перебазируются здесь при получении."""
        fork = int(self.db.jrn_meta()["fork_i"])
        # записи гостя запечатаны прежним K — он здесь ещё сменится (снятый вебхук, новый пакет): открывать ими;
        # at — для ручной диагностики (sqlite3), код его не читает
        self.rsv_put("rsv_c9", {"e": int(guest_e), "F": fork, "upto": fork, "moved": 0, "disputed": [], "total": 0,
                                "at": timeutil.now_iso(), "k": self.reserve_k().hex()})
        log.warning("резерв: правки бота на «%s» эпохи %s после %s — хвостом при встрече", self.reserve_peer_name(),
                    guest_e, fork)

    def reserve_disk_waiting(self) -> bool:
        """Гостя у резерва нет, но его журнал на диске новее того, что здесь уже есть: в С9 —
        перенесённого хвостом, в следовании — применённого (возврат заберёт его, не «пусто»)."""
        keep = self.reserve_peer_state().get("jrn_keep") or {}
        try:
            gj_e, gj_last = int(keep.get("gj_e") or -1), int(keep.get("gj_last") or 0)
        except (TypeError, ValueError):
            return False
        c9 = self.rsv_get("rsv_c9")
        if c9:
            return gj_e == int(c9.get("e") or -2) and gj_last > int(c9.get("upto") or 0)
        if self.reserve_following():
            return gj_last > int(self.db.jrn_meta()["applied"])
        return False

    def reserve_c9_tail_in(self, body: dict, c9: dict) -> list[tuple]:
        """Записи бота на резерве эпохи, которую я сменил: строго подряд после перенесённого —
        перебазировать (держатель — я), подтвердить ему; итог — к Т-Ж4/Т-Ж5 после возврата."""
        from awgbot.util.rsvjrn import event
        line, e = str(body["line"]), int(body["e"])
        try:
            k = bytes.fromhex(str(c9["k"])) if c9.get("k") else self.reserve_k()   # ключ времени его эпохи
            evs = [event.open_event(k, r, line) for r in event.split(bytes.fromhex(str(body["blob"])))]
        except (ValueError, event.RecordError) as ex:
            log.warning("резерв: правки бота на «%s» не открылись: %s", self.reserve_peer_name(), ex)
            return []
        upto = int(c9.get("upto") or 0)
        evs = [ev for ev in evs if ev.i > upto and ev.e == e]
        if not evs:
            return [("send", "jack", {"line": line, "i": upto})]
        if [ev.i for ev in evs] != list(range(upto + 1, upto + 1 + len(evs))):
            return [("send", "jneed", {"line": line, "from": upto + 1})]
        before = self.db.jrn_last_i()

        def mark(conn, rpt) -> None:
            row = conn.execute("SELECT value FROM server_state WHERE key = 'rsv_c9'").fetchone()
            try:
                cur = json.loads(row[0]) if row and row[0] else {}
            except ValueError:
                cur = {}
            if not cur:
                return                                        # возврат уже снял ожидание — не воскрешать
            cur.update(upto=max(int(cur.get("upto") or 0), evs[-1].i), moved=int(cur.get("moved") or 0) + rpt.moved,
                       total=int(cur.get("total") or 0) + len(rpt.disputed),
                       disputed=(list(cur.get("disputed") or []) + rpt.disputed)[:DISPUTED_MAX])
            conn.execute("INSERT INTO server_state (key, value) VALUES ('rsv_c9', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(cur, ensure_ascii=False),))
        try:
            rpt = self.db.jrn_rebase(evs, before_commit=mark)
        except Exception as ex:                                # noqa: BLE001
            log.error("резерв: правки бота на «%s» не перенесены: %s", self.reserve_peer_name(), ex)
            return []
        self._tail_side_effects(before, report=True)   # правки гостя — в сводку Т-РУ5б и «проверь глазами»
        log.info("резерв: правки бота на «%s» %s…%s: перенесено %d, спорных %d, пропущено %d", self.reserve_peer_name(),
                 evs[0].i, evs[-1].i, rpt.moved, len(rpt.disputed), rpt.skipped)
        if rpt.disputed:
            log.info("резерв: правки бота на «%s»: спорные: %s", self.reserve_peer_name(), rpt.disputed)
        return [("send", "jack", {"line": line, "i": evs[-1].i})]

    def reserve_c9_notes(self) -> list[Notification]:
        """После возврата: Т-Ж4/Т-Ж5 по правкам бота на резерве, перенесённым сюда."""
        c9 = self.rsv_get("rsv_c9")
        if not c9:
            return []
        self.rsv_put("rsv_c9", None)
        return self._tail_notes("c9", int(c9.get("moved") or 0), list(c9.get("disputed") or []),
                                int(c9.get("total") or 0))
