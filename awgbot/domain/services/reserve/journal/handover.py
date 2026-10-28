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

from awgbot.util import timeutil

from ...gateway_link.common import log
from ..outbound import RELEASED_WAIT_S


class ReserveHandoverMixin:

    def _release_jrn(self, out: dict) -> dict | None:
        """Передача бота с журналом: всё моё у резерва (подтверждено) — эпоха второй
        стороне одной транзакцией (здесь следование), `released{epoch, last}` без реплики:
        бот основы на резерве догонит события по `last` и возьмёт эпоху."""
        if not self.reserve_jrn_all_acked():
            log.warning("резерв: не все правки подтверждены резервом — передачу бота откладываю")
            return None
        last = self.db.jrn_last_i()
        meta = self.db.jrn_pass(holder="peer", role="follower", why="передача бота", fork_i=last)
        epoch = int((meta or self.db.jrn_meta())["epoch"])
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
        applied = int(self.db.jrn_meta()["applied"])
        if applied < int(last):
            log.warning("резерв: правки бота на «%s» применены по %s из %s — остальное придёт хвостом",
                        self.reserve_peer_name(), applied, last)
        report = dict(out.get("jrn_report") or {})
        report.setdefault("lost", [])
        log.info("резерв: сводка эпохи %s: %s", epoch, json.dumps(report, ensure_ascii=False))
        self.rsv_update("rsv_out", guest_stopped=True, merge_report=report, jrn_report=None)
        self.reserve_monthly_settle()
        self._out_step("take_tg", take_tg_tries=0, take_tg_told=False)   # эпоха — здесь (reserve_epoch_sync)
        return [("take_tg",), ("live",)]
