"""
ship.py — часть пакета journal (см. __init__): своя линия данных к резерву —
события держателя и пачки счётчиков основы.

Направление идёт журналом, когда у обеих сторон `jrn1` (compat.journal_pair):
захват событий включён (`jrn_meta.capture`), пакет на каждое изменение не
собирается — вместо него события пачками ≤ 1 с (такт журнала) и снимок
(`pkg3`, package) по правилам: нет снимка у резерва, разрыв журнала, много
событий после снимка, раз в сутки, смена ключей и частей вне базы.

Сверка до первой отправки в сессии: пока не пришёл `st` резерва с его
взглядом на мою линию (`st.jrn.keep`), не уходит ничего. У резерва номер
больше моего последнего (база потеряла хвост при пропаже питания или
восстановлена) — номера поднимаются выше его (`jrn_bump`) и сначала снимок;
меньше — досылка с его последнего + 1, если эти события у меня ещё есть,
иначе снимок. Подтверждённое резервом (`jack`) удаляется; без подтверждения
дольше RESEND_S — повтор с подтверждённого.

Пачки счётчиков — по таймеру `pkg_minutes` ± 40 % и с ближайшей пачкой
событий; держатель пачек основы — резерв (для открытия) и, в отъезде, бот
основы на резерве (лимит по сумме).
"""
from __future__ import annotations

import random
import time

from awgbot.core import settings
from awgbot.util import pairproto, timeutil

from ...gateway_link.common import log
from .. import compat

EV_LIMIT = 256                    # событий в одной пачке
EV_BYTES = pairproto.CHUNK_BYTES - 4096   # запись целиком в одно сообщение (hex — в MAX_BLOB)
RESEND_S = 20
SNAP_EVENTS = 2000                # событий у резерва после снимка — пора снимок
SNAP_BYTES = 1024 * 1024
SNAP_DAY_S = 24 * 3600
CAPS_TTL_S = 60


class ReserveShipMixin:

    # ── вкл/выкл ────────────────────────────────────────────────────────────
    def _jrn_caps(self) -> list[str]:
        c = self.__dict__.get("_jrn_caps_cache")
        now = time.monotonic()
        if c is None or now - c[0] > CAPS_TTL_S:
            c = (now, compat.caps())
            self.__dict__["_jrn_caps_cache"] = c
        return c[1]

    def reserve_jrn_on(self) -> bool:
        """Моя линия идёт к резерву журналом: пара, `jrn1` у обоих."""
        if not self.reserve_paired():
            return False
        peer = self.reserve_peer_state()
        return compat.journal_pair(self._jrn_caps(), list(peer.get("caps") or []))

    def reserve_jrn_sync(self) -> None:
        """Захват — по направлению; смена — снимок (или пакет прежнего пути) заново.
        У бота основы на резерве захват ставит процесс открытия, а не пара."""
        if getattr(self, "guest", False):
            return
        try:
            changed = self.db.jrn_capture_set(self.reserve_jrn_on())
            self.__dict__["_jrn_capture_on"] = bool(self.db.jrn_meta()["capture"])
        except Exception as e:                                # noqa: BLE001 — база старше журнала (тесты)
            log.debug("резерв: захват журнала не переключён: %s", e)
            return
        if changed:
            on = bool(self.db.jrn_meta()["capture"])
            log.info("резерв: данные для переезда к «%s» — %s", self.reserve_peer_name(),
                     "журналом правок" if on else "пакетом целиком (у резерва нет журнала)")
            self.rsv_put("rsv_jrn_out", None)
            self._jrn_sess_reset()
            self.reserve_pkg_mark()

    def _jrn_touch(self) -> None:
        self.__dict__["_jrn_due_at"] = time.monotonic()

    # ── сессия ──────────────────────────────────────────────────────────────
    def _jrn_sess(self) -> dict:
        s = self.__dict__.get("_jrn_sess_d")
        if s is None:
            s = self.__dict__["_jrn_sess_d"] = {"ok": False, "hold": True, "sent": 0, "sent_at": 0.0, "cnt_sent": 0}
        return s

    def _jrn_sess_reset(self) -> None:
        """Новая сессия пары (или смена направления): ничего не слать до сверки."""
        self.__dict__["_jrn_sess_d"] = None

    def _jrn_out(self) -> dict:
        return self.rsv_get("rsv_jrn_out")

    def _jrn_snap_need(self, why: str) -> None:
        s = self._jrn_sess()
        if not s.get("snap_why"):
            log.info("резерв: журнал к «%s» — сначала снимок (%s)", self.reserve_peer_name(), why)
        s.update(hold=True, snap_why=why)
        self.reserve_pkg_mark()

    def reserve_jrn_keep_in(self, keep: dict | None) -> None:
        """Взгляд резерва на мою линию из его `st` — сверка до первой отправки."""
        if not self.reserve_jrn_on():
            return
        s = self._jrn_sess()
        line = self.db.jrn_line()
        keep = dict(keep or {})
        last = int(keep.get("last") or 0)
        if int(keep.get("size") or 0) > SNAP_BYTES:
            self.reserve_pkg_mark()                           # плановый снимок: у резерва больше мегабайта событий
        if s.get("ok"):
            return
        s["ok"] = True
        mine = self.db.jrn_last_i()
        if str(keep.get("line") or "") != line or not int(keep.get("snap_i") or 0):
            self._jrn_snap_need("у резерва нет снимка этой базы")
            return
        if last > mine:
            self.db.jrn_bump(last)
            self._jrn_snap_need(f"у резерва события до {last}, здесь до {mine}")
            return
        first = self.db.jrn_first_i()
        if last < mine and (not first or first > last + 1):
            self._jrn_snap_need("событий, которых нет у резерва, здесь уже нет")
            return
        self.db.jrn_acked_set(last)
        s.update(hold=False, sent=last, sent_at=0.0, snap_why="")

    def reserve_jrn_snap_ok(self, marks: dict) -> None:
        """Резерв принял снимок `pkg3`: всё по номер снимка у него — подтверждено."""
        i, cs = int(marks.get("i") or 0), int(marks.get("cs") or 0)
        self.db.jrn_acked_set(i)
        self.db.jrn_snap_set(i)
        self.db.jrn_counts_drop(cs)
        out = self._jrn_out()
        self.rsv_put("rsv_jrn_out", {**out, "snap_i": i, "cs": cs, "cnt_acked": max(int(out.get("cnt_acked") or 0), cs),
                                     "snap_at": timeutil.now_iso(), "snap_next": self._jrn_snap_next()})
        s = self._jrn_sess()
        s.update(ok=True, hold=False, sent=max(int(s.get("sent") or 0), i), sent_at=0.0, snap_why="",
                 cnt_sent=max(int(s.get("cnt_sent") or 0), cs))

    @staticmethod
    def _jrn_snap_next() -> str:
        return timeutil.to_iso(timeutil.shift(timeutil.now(), SNAP_DAY_S * random.uniform(0.6, 1.4)))

    def reserve_jrn_snap_due(self) -> bool:
        """Пора ли плановый снимок: много событий у резерва или сутки, если события были."""
        out = self._jrn_out()
        if self.db.jrn_last_i() - int(out.get("snap_i") or 0) > SNAP_EVENTS:
            return True
        nxt = str(out.get("snap_next") or "")
        if not nxt or self.db.jrn_last_i() <= int(out.get("snap_i") or 0):
            return False
        try:
            return timeutil.parse_iso(nxt) <= timeutil.now()
        except ValueError:
            return True

    # ── такт ────────────────────────────────────────────────────────────────
    def reserve_jrn_tick(self) -> list[tuple]:
        """Такт журнала (1 с): события и пачки счётчиков к резерву."""
        self.reserve_jrn_sync()
        if not self.reserve_jrn_on() or not self.reserve_online():
            return []
        s = self._jrn_sess()
        if not s.get("ok") or s.get("hold"):
            return []
        acts: list[tuple] = []
        meta = self.db.jrn_meta()
        now = time.monotonic()
        acked = int(meta["acked"])
        if int(s.get("sent") or 0) > acked and now - float(s.get("sent_at") or 0) > RESEND_S:
            s["sent"] = acked                                  # подтверждения нет — повтор с подтверждённого
        if meta["role"] == "holder":
            acts += self._jrn_events_out(s, int(meta["epoch"]), now)
        if acts or self._jrn_cnt_timer_due(now):
            acts += self._jrn_counts_out(s)
        return acts

    def _jrn_events_out(self, s: dict, epoch: int, now: float) -> list[tuple]:
        from awgbot.util.rsvjrn import event
        evs = [e for e in self.db.jrn_events_after(int(s.get("sent") or 0), EV_LIMIT) if e.e == epoch]
        if not evs:
            return []
        k, line = self.reserve_k(), self.db.jrn_line()
        recs, size = [], 0
        for ev in evs:
            r = event.seal_event(k, line, ev)
            if recs and (size + len(r) > EV_BYTES or ev.i != evs[0].i + len(recs)):
                break
            recs.append(r)
            size += len(r)
        s.update(sent=evs[0].i + len(recs) - 1, sent_at=now)
        return [("send", "jev", {"line": line, "e": epoch, "from": evs[0].i, "n": len(recs),
                                 "blob": b"".join(recs).hex()})]

    def _jrn_cnt_timer_due(self, now: float) -> bool:
        nxt = self.__dict__.get("_jrn_cnt_at")
        if nxt is None:
            self.__dict__["_jrn_cnt_at"] = now + self._jrn_cnt_delay()
            return False
        if now >= nxt:
            self.__dict__["_jrn_cnt_at"] = now + self._jrn_cnt_delay()
            return True
        return False

    @staticmethod
    def _jrn_cnt_delay() -> float:
        return max(1, settings.get_int("app.reserve.pkg_minutes", 30)) * 60 * random.uniform(0.6, 1.4)

    def _jrn_counts_out(self, s: dict, side: str = "a") -> list[tuple]:
        from awgbot.util.rsvjrn import event
        line = self.db.jrn_line()
        after = max(int(s.get("cnt_sent") or 0), int(self._jrn_out().get("cnt_acked") or 0))
        batch = self.db.jrn_counts_fold(f"{line}:{side}", after)
        if batch is None:
            return []
        rec = event.seal_counts(self.reserve_k(), line, side, batch)
        if len(rec) * 2 > pairproto.MAX_BLOB:
            log.warning("резерв: пачка счётчиков %d КБ больше части канала — не уходит", len(rec) // 1024)
            return []
        s["cnt_sent"] = batch.s
        return [("send", "jcnt", {"line": line, "side": side, "s": batch.s, "blob": rec.hex()})]

    def reserve_jrn_flush(self) -> list[tuple]:
        """Передача эпохи: всё неотправленное — сейчас (события и счётчики)."""
        if not self.reserve_jrn_on():
            return []
        s = self._jrn_sess()
        if not s.get("ok") or s.get("hold"):
            return []
        meta = self.db.jrn_meta()
        acts = self._jrn_events_out(s, int(meta["epoch"]), time.monotonic()) if meta["role"] == "holder" else []
        return acts + self._jrn_counts_out(s)

    def reserve_jrn_all_acked(self) -> bool:
        """Резерв подтвердил всё моё (события этой эпохи): эпоху можно передавать."""
        meta = self.db.jrn_meta()
        last = [e for e in self.db.jrn_events_after(int(meta["acked"]), 1) if e.e == int(meta["epoch"])]
        return not last

    # ── ответы резерва на мою линию ─────────────────────────────────────────
    def _jrn_ack_in(self, i: int) -> None:
        self.db.jrn_acked_set(i)
        s = self._jrn_sess()
        if int(s.get("sent") or 0) < i:
            s["sent"] = i

    def _jrn_need_in(self, frm: int) -> None:
        s = self._jrn_sess()
        if frm <= 0:
            self._jrn_snap_need("резерв просит снимок")
            return
        first = self.db.jrn_first_i()
        if first and first <= frm:
            s.update(sent=frm - 1, sent_at=0.0)
        elif frm > self.db.jrn_last_i():
            s.update(sent=self.db.jrn_last_i(), sent_at=0.0)
        else:
            self._jrn_snap_need(f"резерв просит события с {frm}, их здесь уже нет")

    def _jrn_cack_in(self, s_no: int) -> None:
        self.db.jrn_counts_drop(s_no)
        out = self._jrn_out()
        if int(out.get("cnt_acked") or 0) < s_no:
            self.rsv_update("rsv_jrn_out", cnt_acked=int(s_no))
