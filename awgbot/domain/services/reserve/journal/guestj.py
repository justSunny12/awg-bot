"""
guestj.py — часть пакета journal (см. __init__): бот основы на резерве
(гость) с журналом.

Процесс открытия кладёт гостю базу из снимка и событий основы и строку эпохи:
аварийный переезд — гость держатель сразу; переезд кнопкой (и по согласию
основы) — гость следует за основой (её события приходят через бот резерва),
пока основа не передаст бота (`released{epoch, last}`): догнал по `last` —
эпоха его, отметка «ставь вебхук». Основа умерла, не передав бота
(`takeover` от бота резерва), — эпоха + 1 его. Вебхук гость с журналом
ставит только держателем.

Держатель пишет решения; события (захват с секретами и историей — гость) и
пачки счётчиков клона (сторона b) ложатся его журналом данных на диск
резерва (`jrn/` каталога гостя, шифротекст под `K`; вместо `rep.bin`) и
уходят основе по локальному каналу через бот резерва; подтверждает основа
(`jack`, `jcack`). На возврат (`stop`) — последние записи, следование и
`released{epoch, last}` вместо реплики; неподтверждённое своё гость
досылает и после этого — пока основа не подтвердит всё (она не берёт эпоху
без догона).
"""
from __future__ import annotations

import time

from awgbot.util import pairproto

from ...gateway_link.common import log
from .ship import EV_BYTES, EV_LIMIT, RESEND_S, epoch_sane

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
                "last": self.db.jrn_last_i(), "fork": int(meta["fork_i"])}

    def guest_can_take_tg(self) -> bool:
        """Ставить ли вебхук: с журналом — только держателем (следующий ждёт передачи бота)."""
        if not self.guest_jrn_on():
            return True
        return self.db.jrn_meta()["role"] == "holder"

    def _gj(self) -> dict:
        s = self.__dict__.get("_gj_d")
        if s is None:
            meta = self.db.jrn_meta()
            own = int(meta["epoch"]) if meta["role"] == "holder" else 0
            s = self.__dict__["_gj_d"] = {"sent": int(meta["acked"]), "top": self.db.jrn_last_i(), "sent_at": 0.0,
                                          "stored": self.guest_jrn_store().last()[1], "cnt_at": 0.0, "need_at": 0.0,
                                          "released": None, "own_e": own}
        return s

    # ── такт ────────────────────────────────────────────────────────────────
    def guest_jrn_tick(self) -> list[tuple[str, dict]]:
        """Такт журнала гостя: свои события и пачки — основе; догон основы до передачи бота."""
        if not self.guest_jrn_on():
            return []
        s, meta, now = self._gj(), self.db.jrn_meta(), time.monotonic()
        out: list[tuple[str, dict]] = []
        if meta["role"] == "holder":
            s["own_e"] = int(meta["epoch"])
        own = int(s.get("own_e") or 0)
        if own:
            # своё — и после передачи права записи (возврат), пока основа не подтвердит
            if int(s["sent"]) > int(meta["acked"]) and now - float(s["sent_at"]) > RESEND_S:
                s["sent"] = int(meta["acked"])
            out += self._gj_events(s, own, now)
            if out or now - float(s["cnt_at"]) > self._jrn_cnt_delay():
                out += self._gj_counts(now)
        if meta["role"] != "holder":
            out += self._gj_catch_up(s, meta, now)
        return out

    def _gj_events(self, s: dict, epoch: int, now: float) -> list[tuple[str, dict]]:
        from awgbot.util.rsvjrn import event
        evs = self.db.jrn_events_after(int(s["sent"]), EV_LIMIT, epoch=epoch)
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
        last = evs[0].i + len(recs) - 1
        s.update(sent=last, top=max(int(s.get("top") or 0), last), sent_at=now)
        return [("jev", {"line": line, "e": epoch, "from": evs[0].i, "n": len(recs), "blob": b"".join(recs).hex()})]

    def _gj_counts(self, now: float) -> list[tuple[str, dict]]:
        from awgbot.util.rsvjrn import event
        line = self.db.jrn_line()
        self._gj()["cnt_at"] = now
        batch = self.db.jrn_counts_out(f"{line}:b")
        if batch is None:
            return []
        rec = event.seal_counts(self.reserve_k(), line, "b", batch)
        if len(rec) * 2 > pairproto.MAX_BLOB:
            if not self.__dict__.get("_gj_cnt_big"):
                log.warning("гость: пачка счётчиков %d КБ больше части канала — не уходит", len(rec) // 1024)
                self.__dict__["_gj_cnt_big"] = True
            return []
        st = self.guest_jrn_store()
        if batch.bn > st.counts_last("b"):
            st.append_counts("b", [rec])                      # на диск — до отправки (перезагрузка резерва)
        return [("jcnt", {"line": line, "side": "b", "s": batch.bn, "blob": rec.hex()})]

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
        try:
            if kind == "jev":
                replies = self._follow_jev_in(body)
                return [(r[1], r[2]) for r in replies if r[0] == "send" and r[1] == "jneed"]
            if kind == "jcnt":
                self._follow_jcnt_in(body)
                return []
            if kind == "jack":
                i = int(body["i"])
                if i <= int(s.get("top") or 0):
                    self.db.jrn_acked_set(i, drop=False)       # события — и в журнале на диске: jneed досылает из базы
                return []
            if kind == "jneed":
                frm = int(body["from"])
                if 0 < frm <= self.db.jrn_last_i() + 1:
                    s.update(sent=frm - 1, sent_at=0.0)
                return []
            if kind == "jstale":
                e = int(body["e"])
                meta_now = self.db.jrn_meta()
                if meta_now["role"] == "holder" and epoch_sane(e, int(meta_now["epoch"])):
                    meta = self.db.jrn_adopt(e, why="основа взяла эпоху", fork_i=int(meta_now["acked"]))
                    if meta is not None:
                        log.warning("гость: эпоха %s у основы — здесь следование, мои правки после — хвост", meta["epoch"])
                return []
            if kind == "jcack" and str(body.get("side") or "") == "b":
                self.db.jrn_counts_acked(int(body["s"]))
                return []
            if kind == "released":
                s["released"] = {"epoch": int(body["epoch"]), "last": int(body["last"])}
                s["need_at"] = 0.0
                return self._gj_catch_up(s, self.db.jrn_meta(), time.monotonic())
            if kind == "takeover":
                return self._gj_takeover()
        except (KeyError, TypeError, ValueError) as e:
            log.warning("гость: «%s» не того вида: %s", kind, e)
        return []

    def _gj_takeover(self) -> list[tuple[str, dict]]:
        """Основа не передала бота и не на связи (бот резерва решил забрать Telegram сам) —
        эпоха + 1 моя, держатель с последнего применённого."""
        meta = self.db.jrn_meta()
        if meta["role"] == "holder":
            return []
        got = self.db.jrn_pass(holder="self", role="holder", why="основа не передала бота",
                               fork_i=int(meta["applied"]))
        if got is not None:
            log.warning("гость: основа не передала бота — эпоха %s здесь", got["epoch"])
            self._gj()["released"] = None
            self.__dict__["_gj_take_tg"] = True
        return []

    # ── возврат ─────────────────────────────────────────────────────────────
    def guest_jrn_drained(self) -> bool:
        """Основа подтвердила все мои события эпохи, которую я держал."""
        meta = self.db.jrn_meta()
        own = int(self._gj().get("own_e") or 0) or int(meta["epoch"])
        return not self.db.jrn_events_after(int(meta["acked"]), 1, epoch=own)

    def guest_jrn_release(self) -> dict:
        """Возврат: решений больше не пишу (следование), основе — до какого номера моё."""
        meta = self.db.jrn_meta()
        last = self.db.jrn_last_i()
        epoch = int(meta["epoch"])
        if meta["role"] == "holder":
            self._gj()["own_e"] = epoch
            # без права записи, но свои события остаются в базе — досылаются, пока основа не подтвердит
            self.db.jrn_fence(why="возврат: право записи — основе")
        return {"line": self.db.jrn_line(), "epoch": epoch, "last": last}
