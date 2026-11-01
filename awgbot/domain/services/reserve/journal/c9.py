"""
c9.py — часть пакета journal (см. __init__): «вернуть без резерва» (С9) —
правки бота на резерве хвостом.

Основа взяла эпоху сама (`reserve_c9_arm`; отметка прошлого эпизода другой эпохи
заменяется): события бота на резерве эпохи, которую она сменила, приходят его
же записями (живой гость при встрече или возврате; его журнал данных с диска
резерва — резерв шлёт сам) и перебазируются здесь при получении
(`reserve_c9_tail_in`, подтверждение — `jack`); записи запечатаны прежним `K` —
он отложен в отметке `rsv_c9`. Какие правки жду (эпоха и `upto`), основа
говорит в `st.jrn.mine.c9`: гостя у резерва нет — его журнал с диска шлёт
сам резерв (c9deliver), а нет журнала — слово `jc9none`, по которому отметка
снимается фактом (`reserve_c9_none_in`). Пока ждём эти правки
(`rsv_out.await_merge`) — обновление отказывает (tail
`reserve_refuse_update_if_waiting`, сторож терминала — `c9_wait_raw` в tail).
Итог — Т-Ж4/Т-Ж5 после возврата или снятия отметки фактом
(`reserve_c9_notes`); перенос, строки и уведомления — общие с хвостом (tail).
"""
from __future__ import annotations

import json

from awgbot.util import timeutil

from ...gateway_link.common import log
from ...types import Notification
from ..state import HOSTING
from .tail import DISPUTED_MAX


class ReserveC9Mixin:

    def reserve_c9_arm(self, guest_e: int) -> None:
        """Эпоха гостя guest_e взята без его released (роль эпохи, рычаг) — отметка С9 под
        неё, одним правилом для всех мест заведения. Отметка той же эпохи — на месте (повтор
        такта, рычаг после сверки роли: перенесённое не сбрасывать); другой эпохи — прошлый
        эпизод, не снятый фактом: заменяется (момент, F/upto — по новой точке взятия), иначе
        правки нового гостя ушли бы в jstale. Правки гостя прошлой эпохи, не пришедшие
        хвостом, больше не жду — как при «не ждать»; новое ожидание покажет плашка карточки."""
        c9 = self.rsv_get("rsv_c9")
        try:
            same = bool(c9) and int(c9.get("e")) == int(guest_e)
        except (TypeError, ValueError):
            same = False                                      # порченая отметка — не эта эпоха
        if same:
            return
        if c9:
            log.warning("резерв: отметка С9 прошлого эпизода (эпоха %s) заменена на эпоху %s — правки бота на «%s» "
                        "эпохи %s после %s больше не жду (перенесено %s)", c9.get("e"), guest_e, self.reserve_peer_name(),
                        c9.get("e"), c9.get("upto"), c9.get("moved") or 0)
        self.reserve_c9_start(guest_e)

    def reserve_c9_start(self, guest_e: int) -> None:
        """Основа взяла эпоху без резерва: события бота на резерве эпохи guest_e после моей
        точки взятия эпохи — хвост; они придут его же записями (живой гость при встрече или
        возврате, журнал данных гостя с диска резерва — резерв шлёт сам) и перебазируются
        здесь при получении."""
        fork = int(self.db.jrn_meta()["fork_i"])
        # записи гостя запечатаны прежним K — он здесь ещё сменится (снятый вебхук, новый пакет): открывать ими;
        # at_ts — момент заведения для срока «не ждать?» карточки (c9wait), at — то же строкой для диагностики
        now = timeutil.now()
        self.rsv_put("rsv_c9", {"e": int(guest_e), "F": fork, "upto": fork, "moved": 0, "disputed": [], "total": 0,
                                "at": timeutil.to_iso(now), "at_ts": now.timestamp(), "k": self.reserve_k().hex()})
        log.warning("резерв: правки бота на «%s» эпохи %s после %s — хвостом при встрече", self.reserve_peer_name(),
                    guest_e, fork)

    def reserve_c9_st_part(self, meta: dict) -> dict:
        """`st.jrn.mine.c9` — какие правки бота на резерве жду: эпоха гостя и что из неё уже
        перенесено (`upto`). Только пока держатель здесь: правки приходят хвостом С9 только
        держателю, следующему они легли бы событиями держателя (journal/follow)."""
        if getattr(self, "guest", False) or meta.get("role") != "holder" or meta.get("holder") != "self":
            return {}
        c9 = self.rsv_get("rsv_c9")
        try:
            return {"e": int(c9["e"]), "upto": int(c9.get("upto") or 0)} if c9 else {}
        except (KeyError, TypeError, ValueError):
            return {}

    def _c9_home_without_lever(self) -> bool:
        out = self._out()
        return str(out.get("state") or "home") == "home" and not out.get("await_merge")

    def reserve_c9_settle(self) -> list[Notification]:
        """Дома без рычага (срыв переезда после передачи бота, снятый зависший вебхук): отметку
        С9 роль эпохи заводит на случай живого гостя — его правки придут хвостом при встрече.
        Снимается фактом, что ждать их неоткуда: резерв живым словом, сказанным после моего
        перехода домой, гостя не несёт, и его журнал на диске не новее перенесённого (журнал
        новее резерв доставит сам — c9deliver, — отметка ждёт его, а не снимается). По
        одной лишь смене состояния — нет: гость за мёртвым линком мог жить и писать. Итог
        перенесённого — Т-Ж4/Т-Ж5 (`reserve_c9_notes`)."""
        if getattr(self, "guest", False) or not self.rsv_get("rsv_c9"):
            return []
        self.reserve_c9_stamp()                               # отметка прежней версии без момента — отсчёт отсюда
        out = self._out()
        if not self._c9_home_without_lever():
            return []                                         # рычаг: ждёт _tick_await_merge (или _forced_no_merge)
        peer = self.reserve_peer_state()
        if not self._peer_alive(peer) or str(peer.get("inn") or "idle") in HOSTING:
            return []
        if peer.get("at_ts") is not None and out.get("at_ts") is not None:
            fresh = float(peer["at_ts"]) > float(out["at_ts"])
        else:
            fresh = str(peer.get("at") or "") > str(out.get("since") or "")
        if not fresh or self.reserve_disk_waiting():
            return []
        log.info("резерв: «%s» на связи без моего гостя — правок бота на резерве не будет, отметка С9 снята",
                 self.reserve_peer_name())
        return self.reserve_c9_notes()

    def reserve_c9_none_in(self, body: dict) -> list[tuple]:
        """`jc9none{e, last}` резерва: журнала моего бота эпохи e у него после last нет. Отметка
        той же эпохи, всё по last здесь уже перенесено и рычага нет — снять фактом (итог —
        Т-Ж4/Т-Ж5). С рычагом отметку ведёт возврат; last дальше перенесённого — журнал ещё
        идёт (резерв видел мой upto раньше подтверждения) — ждать его."""
        if getattr(self, "guest", False):
            return []
        c9 = self.rsv_get("rsv_c9")
        try:
            e, last = int(body["e"]), int(body["last"])
            same = bool(c9) and int(c9.get("e")) == e
        except (KeyError, TypeError, ValueError):
            return []
        name = self.reserve_peer_name()
        if not same:
            log.info("резерв: «%s»: журнала бота эпохи %s у него нет — здесь такого ожидания нет, пропущено", name, e)
            return []
        if not self._c9_home_without_lever():
            log.info("резерв: «%s»: журнала бота эпохи %s у него нет — отметку С9 ведёт возврат", name, e)
            return []
        upto = int(c9.get("upto") or 0)
        if last > upto:
            log.info("резерв: «%s»: журнал бота эпохи %s у него по %s, здесь перенесено по %s — жду его",
                     name, e, last, upto)
            return []
        log.info("резерв: «%s»: журнала бота эпохи %s после %s у него нет — правок больше не будет, отметка С9 снята "
                 "(перенесено %s)", name, e, upto, int(c9.get("moved") or 0))
        notes = self.reserve_c9_notes()
        return [("notify", notes)] if notes else []

    def reserve_disk_waiting(self) -> bool:
        """Гостя у резерва нет, но его журнал на диске новее того, что здесь уже есть: в С9 —
        перенесённого хвостом (доставляет его резерв сам, c9deliver; отметка не снимается,
        пока он не дошёл), в следовании — применённого (возврат заберёт его, не «пусто»)."""
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
        now_ts = timeutil.now().timestamp()

        def mark(conn, rpt) -> None:
            row = conn.execute("SELECT value FROM server_state WHERE key = 'rsv_c9'").fetchone()
            try:
                cur = json.loads(row[0]) if row and row[0] else {}
            except ValueError:
                cur = {}
            if not cur:
                return                                        # возврат уже снял ожидание — не воскрешать
            if evs[-1].i > int(cur.get("upto") or 0):
                cur["got_ts"] = now_ts                        # журнал идёт — «не ждать?» карточки ждёт (c9wait)
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
