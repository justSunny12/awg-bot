"""
handover.py — часть пакета journal (см. __init__): передача права записи при
переезде кнопкой и возврате, когда направление идёт журналом.

Реплики нет — у второй стороны уже есть всё подтверждённое:

- переезд кнопкой (основа): свои события подтверждены резервом → эпоха второй
  стороне одной транзакцией (здесь следование) → `released{epoch, last}`;
  резерв отдаёт это гостю, тот догоняет события основы по `last`, берёт
  эпоху и ставит вебхук — без перезапуска;
- возврат (гость → резерв → основа): гость дождался подтверждений основы,
  ушёл в следование и сказал `released{epoch, last}`; основа берёт эпоху
  (шаг take_tg) — без слияния и перезапуска, отчёт Т-РУ5б — сводка эпохи.
"""
from __future__ import annotations

import json
import time

from awgbot.util import timeutil

from ...gateway_link.common import log
from ..outbound import RELEASED_WAIT_S

JRN_NEED_S = 10


class ReserveHandoverMixin:

    def _release_jrn(self, out: dict) -> dict | None:
        """Передача бота с журналом: всё моё у резерва (подтверждено) — эпоха второй
        стороне одной транзакцией (здесь следование), `released{epoch, last}` без реплики:
        бот основы на резерве догонит события по `last` и возьмёт эпоху."""
        meta = self.db.jrn_hand_over(why="передача бота")     # проверка и переход — одной транзакцией
        if meta is None:
            log.warning("резерв: не все правки подтверждены резервом — передачу бота откладываю")
            return None
        epoch, last = int(meta["epoch"]), int(meta["fork_i"])
        self._out_step("released", RELEASED_WAIT_S, released_at=timeutil.now_iso(), poll_woken=0)
        self.reserve_set_poll("away", why="released")
        log.info("резерв: бот передан «%s» — эпоха %s, правки по %s", self.reserve_peer_name(), epoch, last)
        return {"gen": int(out.get("gen") or 0), "blob": "", "epoch": epoch, "last": last}

    def reserve_released_jrn_store(self, epoch: int, last: int) -> list[tuple]:
        """`released{epoch, last}` от основы (кнопка, журнал): гостю — он догонит события
        основы по `last`, возьмёт эпоху и поставит вебхук; гость не перезапускается."""
        inn = self._inn()
        if str(inn.get("state") or "") != "taking":
            log.info("резерв: released вне приёма — пропущен")
            return []
        self.rsv_update("rsv_in", replica_at=timeutil.now_iso(), jrn_released={"epoch": int(epoch), "last": int(last)})
        return [("guest", "released", {"line": self.reserve_keep_line(), "epoch": int(epoch), "last": int(last)})]

    def reserve_guest_hello_jrn(self) -> list[tuple]:
        """Гость (пере)подключился к локальному каналу: передача бота от основы, если она
        уже была, — ему снова (она живёт у него только в памяти)."""
        inn = self._inn()
        if str(inn.get("state") or "") not in ("taking", "serving"):
            return []
        if inn.get("tg_take_forced"):
            return [("guest", "takeover", {})]                # основа не передала бота — эпоха гостю
        rel = inn.get("jrn_released")
        if str(inn.get("state") or "") != "taking" or not rel:
            return []
        return [("guest", "released", {"line": self.reserve_keep_line(), "epoch": int(rel["epoch"]),
                                       "last": int(rel["last"])})]

    def reserve_guest_journal_release(self) -> list[tuple]:
        """Возврат, а гостя нет (резерв перезагрузился посреди обслуживания): его журнал
        данных на диске — основе как есть (записи под её K, номера подряд от точки
        расхождения), затем `released{epoch, last}`; основа применит и возьмёт эпоху."""
        from awgbot.util.rsvjrn import event
        from .ship import EV_BYTES
        line = self.reserve_keep_line()
        if not line:
            return []
        st = self.guest_jrn_store()
        recs = [r for r in st.events() if event.head(r).line == line]
        cnts = [r for r in st.counts("b") if event.head(r).line == line]
        if not recs:
            return []                                         # своих событий гость не оставил — прежний ответ
        acts: list[tuple] = []
        group: list[bytes] = []
        size = 0
        for r in recs + [b""]:
            h = event.head(r) if r else None
            if group and (not r or size + len(r) > EV_BYTES or h.e != event.head(group[0]).e
                          or h.n != event.head(group[-1]).n + 1):
                g0 = event.head(group[0])
                acts.append(("send", "jev", {"line": line, "e": g0.e, "from": g0.n, "n": len(group),
                                             "blob": b"".join(group).hex()}))
                group, size = [], 0
            if r:
                group.append(r)
                size += len(r)
        for r in cnts:
            acts.append(("send", "jcnt", {"line": line, "side": "b", "s": event.head(r).n, "blob": r.hex()}))
        last = event.head(recs[-1]) if recs else None
        if last is not None:
            acts.append(("send_released", {"gen": int(self.rsv_get("rsv_pkg_in").get("gen") or 0), "blob": "",
                                           "epoch": last.e, "last": last.n}))
        log.warning("резерв: гостя нет — основе журнал данных гостя с диска: событий %d, пачек %d", len(recs), len(cnts))
        return acts

    def reserve_guest_released_jrn(self, body: dict) -> list[tuple]:
        """Возврат с журналом: гость передал право записи (`released{epoch, last}`) — основе."""
        inn = self._inn()
        if str(inn.get("state") or "") != "releasing":
            return []
        rel = {"epoch": int(body.get("epoch") or 0), "last": int(body.get("last") or 0)}
        self.rsv_update("rsv_in", released_at=timeutil.now_iso(), jrn_back=rel)
        return [("send_released", {"gen": int(inn.get("gen") or 0), "blob": "", **rel})]

    def reserve_released_jrn_in(self, epoch: int, last: int) -> list[tuple]:
        """Возврат с журналом: бот на резерве передал право записи по `last`. Слияния и
        перезапуска нет — база здесь уже несёт его правки (следование); эпоха — сюда
        (шаг take_tg), отчёт Т-РУ5б — сводка эпохи."""
        out = self._out()
        if out.get("state") != "returning" or out.get("step") != "back_req":
            log.info("резерв: released вне возврата — пропущен")
            return []
        if self.db.jrn_meta()["role"] == "holder":
            # «вернуть без резерва» (С9): эпоха и так здесь, правки бота на резерве — хвостом при
            # получении; не всё перенесено — ждать (jneed), по сроку — без недостающего
            c9 = self.rsv_get("rsv_c9")
            if c9 and int(c9.get("upto") or 0) < int(last):
                self.rsv_update("rsv_out", jrn_back={"epoch": int(epoch), "last": int(last), "mono": time.monotonic(),
                                                     "need": time.monotonic(), "c9": True})
                return [("send", "jneed", {"line": self.db.jrn_line(), "from": int(c9.get("upto") or 0) + 1})]
            return self._jrn_back_take(int(epoch), int(last))
        applied = int(self.db.jrn_meta()["applied"])
        if applied < int(last):
            # не догнала: эпоху не беру, пока правки бота на резерве не применены (он ждёт и досылает)
            self.rsv_update("rsv_out", jrn_back={"epoch": int(epoch), "last": int(last), "mono": time.monotonic(),
                                                 "need": time.monotonic()})
            log.info("резерв: бот на «%s» передал право записи по %s, применено по %s — догоняю",
                     self.reserve_peer_name(), last, applied)
            return [("send", "jneed", {"line": self.db.jrn_line(), "from": applied + 1})]
        return self._jrn_back_take(int(epoch), int(last))

    def _jrn_back_caught_up(self) -> list[tuple]:
        """После применения: возврат ждал догона и догнал — эпоха сюда."""
        jb = self.rsv_get("rsv_out").get("jrn_back") if not getattr(self, "guest", False) else None
        if not jb or int(self.db.jrn_meta()["applied"]) < int(jb.get("last") or 0):
            return []
        return self._jrn_back_take(int(jb.get("epoch") or 0), int(jb.get("last") or 0))

    def reserve_jrn_back_tick(self, out: dict) -> list[tuple]:
        """Возврат ждёт догона: просьба досылки раз в JRN_NEED_S; не догнала за срок шага —
        эпоха сюда всё равно (номера — после последнего у бота на резерве), недостающее —
        строкой журнала."""
        jb = dict(out.get("jrn_back") or {})
        now = time.monotonic()
        applied = int(self.rsv_get("rsv_c9").get("upto") or 0) if jb.get("c9") else int(self.db.jrn_meta()["applied"])
        if applied >= int(jb.get("last") or 0):
            return self._jrn_back_take(int(jb.get("epoch") or 0), int(jb.get("last") or 0))
        if now - float(jb.get("mono") or 0) > RELEASED_WAIT_S:
            log.error("резерв: правки бота на «%s» с %s по %s не пришли — возврат без них",
                      self.reserve_peer_name(), applied + 1, jb.get("last"))
            if not jb.get("c9"):
                self.db.jrn_bump(int(jb.get("last") or 0))
            self.rsv_update("rsv_out", jrn_lost=[applied + 1, int(jb.get("last") or 0)])   # в Т-РУ5б — строкой
            return self._jrn_back_take(int(jb.get("epoch") or 0), int(jb.get("last") or 0))
        if now - float(jb.get("need") or 0) > JRN_NEED_S:
            self.rsv_update("rsv_out", jrn_back={**jb, "need": now})
            return [("send", "jneed", {"line": self.db.jrn_line(), "from": applied + 1})]
        return []

    def _jrn_back_take(self, epoch: int, last: int) -> list[tuple]:
        from awgbot.domain.notices import reserve_tail as rt
        out = self._out()
        report = dict(out.get("jrn_report") or {})
        report.setdefault("lost", [])
        lost = list(out.get("jrn_lost") or [])
        if len(lost) == 2 and int(lost[1]) >= int(lost[0]):
            report["lost"].append(rt.jrn_lost(self.reserve_peer_name(), int(lost[0]), int(lost[1])))
        mine = list(out.get("jrn_lost_mine") or [])
        if len(mine) == 2 and int(mine[1]) >= int(mine[0]):
            report["lost"].append(rt.jrn_mine_lost(self.reserve_peer_name(), int(mine[0]), int(mine[1])))
        report.pop("extended", None)
        log.info("резерв: сводка эпохи %s: %s", epoch, json.dumps(report, ensure_ascii=False))
        self.rsv_update("rsv_out", guest_stopped=True, merge_report=report, jrn_report=None, jrn_back=None, jrn_lost=None,
                        jrn_lost_mine=None, tail_skip=None)
        self.reserve_monthly_settle()
        self._out_step("take_tg", take_tg_tries=0, take_tg_told=False)   # эпоха — здесь (reserve_epoch_sync)
        return [("take_tg",), ("live",)]
