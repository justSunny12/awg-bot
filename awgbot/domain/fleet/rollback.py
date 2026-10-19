"""rollback.py — часть пакета fleet (см. __init__): откат набора и
«🔁 Повторить».

Откат — шагами хода в обратном порядке, по одному: шлюз —
`upd_revert` (прежнее у него в удержании, без сети), сервер — `awg-bot
__update_revert` и перезапуск, откат продолжает новый (прежний) процесс.
Каждое состояние отката уже встречалось в ходе вперёд. Хост,
не вернувшийся, — в `revert_failed`, остальные откатываются дальше; итог —
`rollback_failed`, и автоматических команд шлюзам нет до «🔁 Повторить».

Удержание сервера — одно на ход и держит самое первое прежнее (awg-bot.sh),
а ступени сервера могут перемежаться шагами шлюзов (`needs_gw` следующей
ступени). Поэтому сервер откатывается один раз — на первом же его шаге с
конца, сразу на версию до хода (`main_prev`); прочие его шаги откат
пропускает, шлюзы между ними откатываются своим порядком. Сервер старше
агентов на время отката — тот же случай, что понижение при восстановлении
(канал совместим в обе стороны, RELEASING.md §2).
"""
from __future__ import annotations

import logging

from .order import GW_STEP_MAX_S, LOST_AFTER_S, FleetOut, _LOCK, _now_iso, _since
from .state import ROLLBACK_FAILED, ROLLED_BACK, ROLLING_BACK

log = logging.getLogger("awgbot.fleet")


class FleetRollbackMixin:
    """Откат набора и ведение вне хода."""

    # ── откат хода ───────────────────────────────────────────────────────────

    def _fleet_rollback_begin(self, run: dict, out: FleetOut, failed_on, reason: str) -> FleetOut:
        run["state"] = ROLLING_BACK
        run["failed_on"] = failed_on
        run["reason"] = " ".join(str(reason or "").split())[:300]
        self.fleet_run_set(run)
        out.live = True
        log.info("набор: ход до %s — откат (%s: %s)", run.get("tag"), failed_on, run["reason"])
        return self._fleet_backward(run, out)

    def _fleet_backward(self, run: dict, out: FleetOut) -> FleetOut:
        if self._fleet_server_wait(run, out):
            return out                                    # откат сервера ещё идёт — ждём факта
        steps = run.get("steps") or []
        gws = self.fleet_gateways()
        online = {g["slot"]: g for g in gws}
        if self._fleet_expecting(run, gws):
            return out                                    # шлюзы прежнего процесса ещё переподключаются
        while int(run.get("undo") or 0) > 0:
            undo = int(run["undo"])
            step = steps[undo - 1]
            if step[0] == "gw":
                slot = int(step[1])
                st = self.fleet_slot(slot)
                g = online.get(slot)
                if st.get("state") == "reverting":
                    if g is not None and g["online"] and st.get("pending") == "upd_revert":
                        # команда не ушла (сессия упала на отправке) — снова новой сессией
                        out.send.append((slot, "upd_revert", {"id": st["id"], "to": st.get("prev") or run.get("prev")}))
                        return out
                    if g is not None and g["online"] and _since(str(st.get("at") or "")) >= GW_STEP_MAX_S:
                        # на связи, а ответа на откат нет (команда не о его установке —
                        # устройство слота сменили посреди хода, агент её не знает)
                        st["state"], st["error"] = "revert_failed", "шлюз на связи, но не ответил на откат за 30 минут"
                        self.fleet_slot_set(slot, st)
                        self._fleet_revert_answered(run, slot, "revert_failed")
                        continue
                    if g is not None and (g["online"] or _since(str(st.get("at") or "")) < LOST_AFTER_S):
                        return out                        # ждём его ответа (или переподключения — не дольше 15 минут)
                    st["state"] = "done"                  # пропал посреди отката: вернувшись, получит upd_revert
                    self.fleet_slot_set(slot, st)
                    run["undo"] = undo - 1
                    continue
                if g is None or not g["online"] or not st.get("hold"):
                    run["undo"] = undo - 1                # недоступный: доведётся по hello (вне хода)
                    continue
                st["state"], st["pending"] = "reverting", "upd_revert"
                st["at"] = _now_iso()
                self.fleet_slot_set(slot, st)
                self.fleet_run_set(run)
                out.send.append((slot, "upd_revert", {"id": st["id"], "to": st.get("prev") or run.get("prev")}))
                out.live = True
                return out
            # сервер — один откат на ход, сразу на версию до хода (прежнее в
            # удержании — самое первое); прочие его шаги уже откачены этим
            run["undo"] = undo - 1
            if run.get("main_undone"):
                continue
            run["main_undone"] = True
            self._fleet_expect(run, gws)
            self._fleet_server_mark(run, "revert", str(run.get("main_prev") or ""))
            self.fleet_run_set(run)
            out.server, out.live = "revert", True
            return out
        run["state"] = ROLLBACK_FAILED if run.get("revert_failed") else ROLLED_BACK
        run["at"] = _now_iso()
        self.fleet_run_set(run)
        out.live = out.final = True
        log.info("набор: откат хода до %s — %s", run.get("tag"), run["state"])
        return out

    def _fleet_revert_answered(self, run: dict, slot: int, state: str) -> None:
        steps = run.get("steps") or []
        undo = int(run.get("undo") or 0)
        if undo and steps[undo - 1][0] == "gw" and int(steps[undo - 1][1]) == slot:
            run["undo"] = undo - 1
        st = self.fleet_slot(slot)
        st["hold"] = False
        self.fleet_slot_set(slot, st)
        if state == "revert_failed" and slot not in run.setdefault("revert_failed", []):
            run["revert_failed"].append(slot)
        self.fleet_run_set(run)

    # ── «🔁 Повторить» ───────────────────────────────────────────────────────

    def fleet_retry(self) -> str:
        """Набор откатился или откат не прошёл — ход заново с того, что стоит;
        догоны, не прошедшие трижды, — заново. Пусто — начато; иначе причина."""
        with _LOCK:
            for gw in self.db.gateways():
                st = self.fleet_slot(gw.id)
                if not st.get("run") and st.get("state") in ("failed", "rolled_back", "refused"):
                    st["tries"], st["next_at"], st["final"] = 0, "", False
                    self.fleet_slot_set(gw.id, st)
            run = self.fleet_run()
            if run.get("state") not in (ROLLED_BACK, ROLLBACK_FAILED):
                return ""
            target = self.fleet_target()
            if target is None:
                return "nothing"
            run["state"] = ""                             # ход закрыт — новый с того, что стоит
            self.fleet_run_set(run)
            return self.fleet_start(target.tag)
