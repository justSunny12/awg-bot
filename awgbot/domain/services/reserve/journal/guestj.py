"""
guestj.py — часть пакета journal (см. __init__): бот основы на резерве
(гость) с журналом.

Процесс открытия кладёт гостю базу из снимка и событий основы и строку эпохи:
аварийный переезд — гость держатель сразу; переезд кнопкой — гость следует
за основой (её события приходят через бот резерва), пока основа не передаст
бота (`released{epoch, last}`): догнал по `last` — эпоха его, отметка «ставь
вебхук».

Держатель пишет решения; события (захват с секретами и историей — гость) и
пачки счётчиков клона (сторона b) ложатся его журналом данных на диск
резерва (`jrn/` каталога гостя, шифротекст под `K`; вместо `rep.bin`) и
уходят основе по локальному каналу через бот резерва; подтверждает основа
(`jack`, `jcack`). На возврат (`stop`) — последние записи, ожидание
подтверждений, следование и `released{epoch, last}` вместо реплики.
"""
from __future__ import annotations

import time

from awgbot.util import pairproto

from ...gateway_link.common import log
from .ship import EV_BYTES, EV_LIMIT, RESEND_S

NEED_EVERY_S = 10


class ReserveGuestJournalMixin:

    def guest_jrn_store(self):
        from awgbot.infra import rsvzone
        from awgbot.util.rsvjrn.segments import Store
        return Store(rsvzone.VAR_DIR / "jrn")

    def guest_jrn_on(self) -> bool:
        try:
            return bool(self.guest and self.db.jrn_meta()["capture"])
        except Exception:                                     # noqa: BLE001 — база старше журнала
            return False

    def guest_jrn_status(self) -> dict:
        """Эпоха и номера гостя — боту резерва (в его `st.jrn.keep.guest_e`)."""
        if not self.guest_jrn_on():
            return {}
        meta = self.db.jrn_meta()
        return {"e": int(meta["epoch"]), "role": str(meta["role"]), "applied": int(meta["applied"]),
                "last": self.db.jrn_last_i()}

    def _gj(self) -> dict:
        s = self.__dict__.get("_gj_d")
        if s is None:
            meta = self.db.jrn_meta()
            s = self.__dict__["_gj_d"] = {"sent": int(meta["acked"]), "sent_at": 0.0, "stored": self.guest_jrn_store().last()[1],
                                        "cnt_sent": 0, "cnt_at": 0.0, "need_at": 0.0, "released": None}
        return s

    # ── такт ────────────────────────────────────────────────────────────────
    def guest_jrn_tick(self) -> list[tuple[str, dict]]:
        """Такт журнала гостя: свои события и пачки — основе; догон основы до передачи бота."""
        if not self.guest_jrn_on():
            return []
        s, meta, now = self._gj(), self.db.jrn_meta(), time.monotonic()
        out: list[tuple[str, dict]] = []
        if meta["role"] == "holder":
            if int(s["sent"]) > int(meta["acked"]) and now - float(s["sent_at"]) > RESEND_S:
                s["sent"] = int(meta["acked"])
            out += self._gj_events(s, int(meta["epoch"]), now)
            if out or now - float(s["cnt_at"]) > self._jrn_cnt_delay():
                out += self._gj_counts(s, now)
        else:
            out += self._gj_catch_up(s, meta, now)
        return out

    def _gj_events(self, s: dict, epoch: int, now: float) -> list[tuple[str, dict]]:
        from awgbot.util.rsvjrn import event
        evs = [e for e in self.db.jrn_events_after(int(s["sent"]), EV_LIMIT) if e.e == epoch]
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
        new = [r for r in recs if event.head(r).n > int(s["stored"])]
        if new:
            self.guest_jrn_store().append_events(new)         # журнал данных гостя на диске — до отправки
            s["stored"] = event.head(new[-1]).n
        s.update(sent=evs[0].i + len(recs) - 1, sent_at=now)
        return [("jev", {"line": line, "e": epoch, "from": evs[0].i, "n": len(recs), "blob": b"".join(recs).hex()})]

    def _gj_counts(self, s: dict, now: float) -> list[tuple[str, dict]]:
        from awgbot.util.rsvjrn import event
        line = self.db.jrn_line()
        s["cnt_at"] = now
        batch = self.db.jrn_counts_fold(f"{line}:b", int(s["cnt_sent"]))
        if batch is None:
            return []
        rec = event.seal_counts(self.reserve_k(), line, "b", batch)
        if len(rec) * 2 > pairproto.MAX_BLOB:
            log.warning("гость: пачка счётчиков %d КБ больше части канала — не уходит", len(rec) // 1024)
            return []
        self.guest_jrn_store().append_counts("b", [rec])
        s["cnt_sent"] = batch.s
        return [("jcnt", {"line": line, "side": "b", "s": batch.s, "blob": rec.hex()})]

    def _gj_catch_up(self, s: dict, meta: dict, now: float) -> list[tuple[str, dict]]:
        """Следую за основой (переезд кнопкой): передача бота пришла и догнал — эпоха моя."""
        rel = s.get("released")
        if rel and int(meta["applied"]) >= int(rel["last"]):
            got = self.db.jrn_pass(holder="self", role="holder", why="передача бота", fork_i=int(rel["last"]),
                                   epoch=int(rel["epoch"]))
            s["released"] = None
            s.update(sent=int(rel["last"]), stored=max(int(s["stored"]), int(rel["last"])))
            if got is not None:
                log.info("гость: эпоха %s здесь — пишу решения (передача бота по %s)", got["epoch"], rel["last"])
                self.__dict__["_gj_take_tg"] = True
            return []
        if rel and now - float(s["need_at"]) > NEED_EVERY_S:
            s["need_at"] = now
            return [("jneed", {"line": self.db.jrn_line(), "from": int(meta["applied"]) + 1})]
        return []

    def guest_jrn_take_tg(self) -> bool:
        """Эпоха взята после передачи бота — пора ставить вебхук (один раз)."""
        return bool(self.__dict__.pop("_gj_take_tg", False))

    # ── от основы (через бот резерва) ───────────────────────────────────────
    def guest_jrn_in(self, kind: str, body: dict) -> list[tuple[str, dict]]:
        if not self.guest_jrn_on():
            return []
        s = self._gj()
        if kind == "jev":
            replies = self._follow_jev_in(body)
            return [(r[1], r[2]) for r in replies if r[0] == "send" and r[1] == "jneed"]
        if kind == "jcnt":
            self._follow_jcnt_in(body)
            return []
        if kind == "jack":
            self.db.jrn_acked_set(int(body["i"]), drop=False)  # события — и в журнале на диске: jneed досылает из базы
            return []
        if kind == "jneed":
            frm = int(body["from"])
            s.update(sent=max(0, frm - 1), sent_at=0.0)
            return []
        if kind == "jstale":
            meta = self.db.jrn_adopt(int(body["e"]), why="основа взяла эпоху")
            if meta is not None:
                log.warning("гость: эпоха %s у основы — здесь следование, мои правки после — хвост", meta["epoch"])
            return []
        if kind == "jcack" and str(body.get("side") or "") == "b":
            self.db.jrn_counts_drop(int(body["s"]))
            return []
        if kind == "released":
            s["released"] = {"epoch": int(body["epoch"]), "last": int(body["last"])}
            s["need_at"] = 0.0
            return self._gj_catch_up(s, self.db.jrn_meta(), time.monotonic())
        return []

    # ── возврат ─────────────────────────────────────────────────────────────
    def guest_jrn_drained(self) -> bool:
        """Основа подтвердила все мои события этой эпохи."""
        meta = self.db.jrn_meta()
        return not [e for e in self.db.jrn_events_after(int(meta["acked"]), 1) if e.e == int(meta["epoch"])]

    def guest_jrn_release(self) -> dict:
        """Возврат: решений больше не пишу (следование), основе — до какого номера моё."""
        meta = self.db.jrn_meta()
        last = self.db.jrn_last_i()
        epoch = int(meta["epoch"])
        self.db.jrn_pass(holder="peer", role="follower", why="возврат", fork_i=last, epoch=epoch)
        return {"line": self.db.jrn_line(), "epoch": epoch, "last": last}
